"""Local ONNX fraud scoring from Redis-backed prior card history."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import onnxruntime as ort
import redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from payguard_fraud.features import FEATURES, FeatureBuilder, history_from_dict, history_to_dict
from payguard_fraud.score_metrics import ScoreMetrics
from payguard_fraud.simulator import Event


class ScoreRequest(BaseModel):
    auth_id: str = Field(alias="authId")
    card_id: str = Field(alias="cardId")
    merchant_id: str = Field(alias="merchantId")
    mcc: str
    amount_minor: int = Field(alias="amountMinor", gt=0)
    device_id: str = Field(alias="deviceId")
    latitude: float = 1.35
    longitude: float = 103.82
    home_latitude: float = Field(1.35, alias="homeLatitude")
    home_longitude: float = Field(103.82, alias="homeLongitude")
    merchant_risk: float = Field(0.1, alias="merchantRisk", ge=0, le=1)
    prior_refund_ratio: float = Field(0.0, alias="priorRefundRatio", ge=0, le=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


def to_event(request: ScoreRequest) -> Event:
    return Event(request.auth_id, request.timestamp, request.card_id, request.merchant_id,
                 request.mcc, request.amount_minor, request.device_id, request.latitude,
                 request.longitude, request.home_latitude, request.home_longitude,
                 request.merchant_risk, request.prior_refund_ratio, 0, None)


class FeatureStore:
    def __init__(self, client: redis.Redis):
        self.client = client

    def builder(self, card_id: str) -> FeatureBuilder:
        builder = FeatureBuilder()
        payload = self.client.get(f"payguard:history:{card_id}")
        if payload:
            builder.cards[card_id] = history_from_dict(json.loads(payload))
        return builder

    def observe(self, event: Event) -> bool:
        builder = self.builder(event.card_id)
        builder.transform_one(event)
        history = json.dumps(history_to_dict(builder.cards[event.card_id]))
        # One Redis script records both the new history and seen ID or neither.
        script = """
        if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
        redis.call('SET', KEYS[1], ARGV[1])
        redis.call('SET', KEYS[2], '1', 'EX', 2592000)
        return 1
        """
        return bool(self.client.eval(script, 2,
            f"payguard:history:{event.card_id}", f"payguard:seen:{event.transaction_id}", history))


EXPLAIN_POLICIES = ("always", "declines")


class Scorer:
    def __init__(self, artifacts: Path, store: FeatureStore, threads: int = 0,
                 explain: str = "always"):
        """threads > 0 caps ONNX Runtime and LightGBM to that many threads per request.

        The default (0) lets both libraries use every core, which is fastest for one
        process but oversubscribes the CPU when several worker processes run.

        explain="declines" computes reason codes only when the score reaches the
        decline threshold, which is also where a case opens. TreeSHAP is most of the
        scorer's own CPU, and below the threshold the codes are not shown anywhere.
        """
        if explain not in EXPLAIN_POLICIES:
            raise ValueError(f"explain must be one of {EXPLAIN_POLICIES}, got {explain!r}")
        self.store = store
        self.threads = threads
        self.explain = explain
        self.model = joblib.load(artifacts / "lightgbm_model.joblib")
        self.calibration = joblib.load(artifacts / "lightgbm_calibration.joblib")
        options = ort.SessionOptions()
        if threads > 0:
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(artifacts / "lightgbm.onnx"), options,
                                            providers=["CPUExecutionProvider"])
        self.threshold = json.loads((artifacts / "synthetic_results.json").read_text())[
            "lightgbm"]["cost_threshold"]

    def score(self, request: ScoreRequest) -> dict:
        event = to_event(request)
        builder = self.store.builder(event.card_id)
        features = builder.transform_one(event, update=False)
        vector = np.array([[features[name] for name in FEATURES]], dtype=np.float32)
        output = self.session.run(None, {self.session.get_inputs()[0].name: vector})
        raw = float(np.asarray(output[1])[0, 1])
        probability = float(self.calibration.predict([raw])[0])
        decline = probability >= self.threshold
        codes = self.reason_codes(vector) if decline or self.explain == "always" else []
        return {
            "score": probability,
            "wouldDecline": decline,
            "threshold": self.threshold,
            "reasonCodes": codes,
            "modelVersion": "synthetic-lightgbm-v1",
        }

    def reason_codes(self, vector: np.ndarray) -> list[str]:
        """Top three features pushing the score up, from LightGBM's TreeSHAP."""
        extra = {"num_threads": self.threads} if self.threads > 0 else {}
        contributions = self.model.booster_.predict(vector, pred_contrib=True, **extra)[0][:-1]
        positive = sorted(((FEATURES[i], float(value)) for i, value in enumerate(contributions)
                           if value > 0), key=lambda pair: pair[1], reverse=True)
        return [name for name, _ in positive[:3]]


@lru_cache
def scorer() -> Scorer:
    artifacts = Path(os.getenv("PAYGUARD_MODEL_DIR", "artifacts"))
    client = redis.Redis.from_url(os.getenv("PAYGUARD_REDIS_URL", "redis://127.0.0.1:16379/0"),
                                  socket_connect_timeout=0.02, socket_timeout=0.02)
    return Scorer(artifacts, FeatureStore(client),
                  threads=int(os.getenv("PAYGUARD_SCORER_THREADS", "0")),
                  explain=os.getenv("PAYGUARD_EXPLAIN", "always"))


app = FastAPI(title="PayGuard local fraud scoring")
metrics = ScoreMetrics(Path(os.getenv("PAYGUARD_MODEL_DIR", "artifacts")) / "score_baseline.json")


@app.get("/metrics", response_class=PlainTextResponse)
def metric_text() -> str:
    return metrics.render()


@app.get("/health")
def health() -> dict:
    try:
        scorer().store.client.ping()
    except Exception as exc:
        raise HTTPException(503, "model or feature store unavailable") from exc
    return {"status": "ok"}


@app.post("/score")
def score(request: ScoreRequest) -> dict:
    started = time.perf_counter()
    try:
        result = scorer().score(request)
        metrics.observe(time.perf_counter() - started, result["score"])
        return result
    except (OSError, redis.RedisError) as exc:
        metrics.observe(time.perf_counter() - started, None)
        raise HTTPException(503, "model or feature store unavailable") from exc


@app.post("/observe")
def observe(request: ScoreRequest) -> dict:
    try:
        return {"accepted": scorer().store.observe(to_event(request))}
    except redis.RedisError as exc:
        raise HTTPException(503, "feature store unavailable") from exc
