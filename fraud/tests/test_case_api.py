"""Server-side analyst identity on the case API (local and proxy modes)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from payguard_fraud import case_api
from payguard_fraud.case_store import CaseStore

TOKEN = "test-proxy-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYGUARD_CASE_DB", str(tmp_path / "cases.sqlite"))
    monkeypatch.delenv("PAYGUARD_CASE_DSN", raising=False)
    monkeypatch.setenv("PAYGUARD_OLLAMA_URL", "http://127.0.0.1:9/api/generate")
    for name in ("PAYGUARD_AUTH_MODE", "PAYGUARD_PROXY_TOKEN", "PAYGUARD_LOCAL_ROLE",
                 "PAYGUARD_LOCAL_ANALYST"):
        monkeypatch.delenv(name, raising=False)
    case_api.store.cache_clear()
    CaseStore(tmp_path / "cases.sqlite").observe({
        "timestamp": "2026-10-11T08:00:00Z",
        "request": {"authId": "auth_1", "cardId": "card_1", "merchantId": "shop",
                    "amountMinor": 100, "deviceId": "dev"},
        "result": {"status": "DECLINED", "modelScore": 0.95, "modelReasonCodes": ["x"]}})
    yield TestClient(case_api.app)
    case_api.store.cache_clear()


def headers(user: str | None = "ana", role: str | None = "analyst",
            token: str | None = TOKEN) -> dict:
    values = {"X-PayGuard-User": user, "X-PayGuard-Role": role,
              "X-PayGuard-Proxy-Token": token}
    return {key: value for key, value in values.items() if value is not None}


def test_local_mode_is_a_single_lead_identity(client) -> None:
    me = client.get("/me").json()
    assert (me["name"], me["role"], me["mode"]) == ("local_analyst", "lead", "local")
    assert client.post("/cases/case_auth_1/draft").status_code == 200
    reply = client.post("/cases/case_auth_1/review",
                        json={"decision": "APPROVED", "note": "ok", "analyst": "mallory"})
    assert reply.status_code == 200
    assert reply.json()["analyst"] == "local_analyst"  # A body name is ignored.
    detail = client.get("/cases/case_auth_1").json()
    assert detail["analyst"] == "local_analyst"
    assert [row["analyst"] for row in detail["reviews"]] == ["local_analyst"]


def test_proxy_mode_rejects_missing_or_wrong_token_and_unknown_role(client, monkeypatch) -> None:
    monkeypatch.setenv("PAYGUARD_AUTH_MODE", "proxy")
    monkeypatch.setenv("PAYGUARD_PROXY_TOKEN", TOKEN)
    assert client.get("/cases", headers=headers(token=None)).status_code == 401
    assert client.get("/cases", headers=headers(token="guess")).status_code == 401
    assert client.get("/cases", headers=headers(role="admin")).status_code == 403
    assert client.get("/cases", headers=headers(user=None)).status_code == 403
    assert client.get("/cases", headers=headers()).status_code == 200


def test_proxy_mode_without_configured_token_denies_everyone(client, monkeypatch) -> None:
    monkeypatch.setenv("PAYGUARD_AUTH_MODE", "proxy")
    assert client.get("/me", headers=headers(token="")).status_code == 401


def test_analyst_drafts_lead_reviews_and_audit_names_both(client, monkeypatch) -> None:
    monkeypatch.setenv("PAYGUARD_AUTH_MODE", "proxy")
    monkeypatch.setenv("PAYGUARD_PROXY_TOKEN", TOKEN)
    analyst, lead = headers(), headers(user="lee", role="lead")
    assert client.post("/cases/case_auth_1/draft", headers=analyst).status_code == 200
    denied = client.post("/cases/case_auth_1/review", headers=analyst,
                         json={"decision": "APPROVED", "note": ""})
    assert denied.status_code == 403
    assert client.get("/audit", headers=analyst).status_code == 403
    ok = client.post("/cases/case_auth_1/review", headers=lead,
                     json={"decision": "REJECTED", "note": "customer travel"})
    assert ok.status_code == 200 and ok.json()["analyst"] == "lee"
    again = client.post("/cases/case_auth_1/review", headers=lead,
                        json={"decision": "APPROVED", "note": ""})
    assert again.status_code == 400
    assert client.post("/cases/case_auth_1/draft", headers=analyst).status_code == 409
    log = client.get("/audit", headers=lead, params={"case_id": "case_auth_1"}).json()
    actions = [(row["actor"], row["action"]) for row in reversed(log)]
    assert ("ana", "draft_saved") in actions and ("lee", "review_recorded") in actions
    assert ("ana", "view_case") in actions


def test_unknown_case_is_404(client) -> None:
    assert client.get("/cases/case_missing").status_code == 404
    assert client.post("/cases/case_missing/draft").status_code == 404
