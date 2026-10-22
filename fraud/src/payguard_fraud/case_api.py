"""Local analyst case queue; recommendations never mutate a card.

Identity comes from the server's configuration, never from a request body:

* ``PAYGUARD_AUTH_MODE=proxy``: an authenticating reverse proxy sets
  ``X-PayGuard-User`` and ``X-PayGuard-Role`` plus ``X-PayGuard-Proxy-Token``,
  which must equal ``PAYGUARD_PROXY_TOKEN``. Anything else is 401 or 403.
* ``local`` (default): one single-user development identity from
  ``PAYGUARD_LOCAL_ANALYST`` (default ``local_analyst``) and
  ``PAYGUARD_LOCAL_ROLE`` (default ``lead``, so the existing local demo can
  record reviews). Local mode authenticates nobody; bind it to 127.0.0.1.
"""

from __future__ import annotations

import hmac
import json
import os
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from payguard_fraud.case_agent import build_graph
from payguard_fraud.case_store import ROLES, Actor, CaseStore, Forbidden

TOKEN_HEADER = "x-payguard-proxy-token"


class ReviewRequest(BaseModel):
    decision: str
    note: str = ""


@lru_cache
def store() -> CaseStore:
    return CaseStore.from_env()


def current_actor(request: Request) -> Actor:
    mode = os.getenv("PAYGUARD_AUTH_MODE", "local")
    if mode == "local":
        return Actor(os.getenv("PAYGUARD_LOCAL_ANALYST", "local_analyst"),
                     os.getenv("PAYGUARD_LOCAL_ROLE", "lead"))
    if mode != "proxy":
        raise HTTPException(500, "PAYGUARD_AUTH_MODE must be local or proxy")
    expected = os.getenv("PAYGUARD_PROXY_TOKEN", "")
    supplied = request.headers.get(TOKEN_HEADER, "")
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "request did not come through the trusted proxy")
    user = request.headers.get("x-payguard-user", "").strip()
    role = request.headers.get("x-payguard-role", "").strip().lower()
    if not user or role not in ROLES:
        raise HTTPException(403, "no PayGuard analyst role for this user")
    return Actor(user, role)


CurrentActor = Annotated[Actor, Depends(current_actor)]
app = FastAPI(title="PayGuard local analyst cases")


@app.exception_handler(Forbidden)
async def forbidden(_request: Request, exc: Forbidden):
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": str(exc)}, status_code=403)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/me")
def me(actor: CurrentActor) -> dict:
    return {"name": actor.name, "role": actor.role,
            "permissions": sorted(ROLES[actor.role]),
            "mode": os.getenv("PAYGUARD_AUTH_MODE", "local")}


@app.get("/cases")
def list_cases(actor: CurrentActor) -> list[dict]:
    return store().list_cases(actor)


@app.get("/cases/{case_id}")
def get_case(case_id: str, actor: CurrentActor) -> dict:
    try:
        case = store().case(case_id, actor)
        case["cardHistory"] = store().get_card_history(case_id)
        case["reasonCodes"] = store().get_reason_codes(case_id)
        case["merchantProfile"] = store().get_merchant_profile(case_id)
        case["linkedCards"] = store().get_linked_cards(case_id)
        case["similarCases"] = store().get_similar_cases(case_id)
        case["reviews"] = store().reviews(case_id)
        if case["draft"]:
            case["draft"] = json.loads(case["draft"])
        return case
    except KeyError as exc:
        raise HTTPException(404, "case not found") from exc


@app.post("/cases/{case_id}/draft")
def draft(case_id: str, actor: CurrentActor) -> dict:
    actor.require("draft")
    try:
        store().case(case_id, actor)
        model = os.getenv("PAYGUARD_CASE_MODEL", "qwen2.5:0.5b")
        result = build_graph(store(), model=model).invoke({"case_id": case_id})["result"]
        store().save_draft(case_id, result, actor)
        return result
    except KeyError as exc:
        raise HTTPException(404, "case not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/cases/{case_id}/review")
def review(case_id: str, request: ReviewRequest, actor: CurrentActor) -> dict:
    try:
        store().review(case_id, request.decision, actor, request.note)
        return {"caseId": case_id, "status": request.decision, "analyst": actor.name}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/audit")
def audit(actor: CurrentActor, case_id: str | None = None, limit: int = 200) -> list[dict]:
    return store().audit_log(actor, case_id, limit)
