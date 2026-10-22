"""Capping scorer threads must not change any score (needs local model artifacts)."""

from __future__ import annotations

from pathlib import Path

import pytest

from payguard_fraud.features import FeatureBuilder
from payguard_fraud.score_api import Scorer, ScoreRequest

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"
pytestmark = pytest.mark.skipif(not (ARTIFACTS / "lightgbm.onnx").exists(),
                                reason="model artifacts are generated locally, not in Git")


class EmptyHistory:
    def builder(self, card_id: str) -> FeatureBuilder:
        return FeatureBuilder()


def request(index: int) -> ScoreRequest:
    return ScoreRequest.model_validate({
        "authId": f"auth_t_{index}", "cardId": "card_t", "merchantId": f"m{index}",
        "mcc": ["5411", "7995", "5812"][index % 3], "amountMinor": [120, 4999, 80000][index % 3],
        "merchantRisk": [0.05, 0.6, 0.9][index % 3], "deviceId": f"dev{index}",
        "country": "SG", "currency": "SGD", "timestamp": "2026-10-22T03:00:00Z"})


def test_single_thread_scores_equal_default_scores() -> None:
    default = Scorer(ARTIFACTS, EmptyHistory())
    capped = Scorer(ARTIFACTS, EmptyHistory(), threads=1)
    assert capped.session.get_session_options().intra_op_num_threads == 1
    for index in range(6):
        a, b = default.score(request(index)), capped.score(request(index))
        assert a["score"] == pytest.approx(b["score"], abs=1e-9)
        assert (a["wouldDecline"], a["reasonCodes"]) == (b["wouldDecline"], b["reasonCodes"])
