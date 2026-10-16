"""Local analyst case queue; recommendations never mutate a card."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from payguard_fraud.case_agent import build_graph
from payguard_fraud.case_store import CaseStore


class ReviewRequest(BaseModel):
    decision: str
    analyst: str = Field(min_length=1)
    note: str = ""


@lru_cache
def store() -> CaseStore:
    return CaseStore(Path(os.getenv("PAYGUARD_CASE_DB", "data/cases.sqlite")))


app = FastAPI(title="PayGuard local analyst cases")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/cases")
def list_cases() -> list[dict]:
    return store().list_cases()


@app.get("/cases/{case_id}")
def get_case(case_id: str) -> dict:
    try:
        case = store().case(case_id)
        case["cardHistory"] = store().get_card_history(case_id)
        case["reasonCodes"] = store().get_reason_codes(case_id)
        case["merchantProfile"] = store().get_merchant_profile(case_id)
        case["linkedCards"] = store().get_linked_cards(case_id)
        case["similarCases"] = store().get_similar_cases(case_id)
        if case["draft"]:
            case["draft"] = json.loads(case["draft"])
        return case
    except KeyError as exc:
        raise HTTPException(404, "case not found") from exc


@app.post("/cases/{case_id}/draft")
def draft(case_id: str) -> dict:
    try:
        store().case(case_id)
        model = os.getenv("PAYGUARD_CASE_MODEL", "qwen2.5:0.5b")
        result = build_graph(store(), model=model).invoke({"case_id": case_id})["result"]
        store().save_draft(case_id, result)
        return result
    except KeyError as exc:
        raise HTTPException(404, "case not found") from exc


@app.post("/cases/{case_id}/review")
def review(case_id: str, request: ReviewRequest) -> dict:
    try:
        store().review(case_id, request.decision, request.analyst, request.note)
        return {"caseId": case_id, "status": request.decision}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
