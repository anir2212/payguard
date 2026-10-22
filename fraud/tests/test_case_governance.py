"""Analyst roles, review history, audit trail, retention and schema upgrade."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from payguard_fraud.case_store import MIN_AUDIT_RETENTION_DAYS, Actor, CaseStore, Forbidden

ANALYST = Actor("ana", "analyst")
LEAD = Actor("lee", "lead")
DRAFT = {"pattern": "card_testing", "recommendedAction": "freeze_card",
         "draftSource": "rules_fallback", "claims": []}


def event(auth_id: str, score: float = 0.95) -> dict:
    return {"timestamp": "2026-10-11T08:00:00Z",
            "request": {"authId": auth_id, "cardId": "card_1", "merchantId": "shop",
                        "amountMinor": 100, "deviceId": "dev"},
            "result": {"status": "DECLINED", "modelScore": score, "modelReasonCodes": []}}


def store_with_case(tmp_path) -> CaseStore:
    store = CaseStore(tmp_path / "cases.sqlite")
    assert store.observe(event("auth_1")) == "case_auth_1"
    return store


def test_actor_validation() -> None:
    with pytest.raises(ValueError):
        Actor(" ", "lead")
    with pytest.raises(ValueError):
        Actor("x", "admin")


def test_analyst_can_view_and_draft_but_not_review_or_read_audit(tmp_path) -> None:
    store = store_with_case(tmp_path)
    assert store.list_cases(ANALYST)[0]["case_id"] == "case_auth_1"
    store.save_draft("case_auth_1", DRAFT, ANALYST)
    assert store.case("case_auth_1", ANALYST)["drafted_by"] == "ana"
    with pytest.raises(Forbidden):
        store.review("case_auth_1", "APPROVED", ANALYST, "")
    with pytest.raises(Forbidden):
        store.audit_log(ANALYST)
    assert store.case("case_auth_1")["status"] == "OPEN"


def test_review_is_recorded_once_with_history_and_audit(tmp_path) -> None:
    store = store_with_case(tmp_path)
    store.save_draft("case_auth_1", DRAFT, ANALYST)
    with pytest.raises(ValueError, match="decision"):
        store.review("case_auth_1", "FREEZE", LEAD, "")
    store.review("case_auth_1", "REJECTED", LEAD, "known customer travel")
    case = store.case("case_auth_1")
    assert (case["status"], case["analyst"], case["analyst_note"]) == (
        "REJECTED", "lee", "known customer travel")
    assert case["reviewed_at"]
    with pytest.raises(ValueError, match="already reviewed"):
        store.review("case_auth_1", "APPROVED", LEAD, "")
    with pytest.raises(ValueError, match="already reviewed"):
        store.save_draft("case_auth_1", DRAFT, ANALYST)
    history = store.reviews("case_auth_1")
    assert [(row["decision"], row["analyst"], row["role"]) for row in history] == [
        ("REJECTED", "lee", "lead")]
    actions = [(row["actor"], row["action"]) for row in reversed(store.audit_log(LEAD))]
    assert actions == [("system:case-stream", "case_opened"), ("ana", "draft_saved"),
                       ("lee", "review_recorded")]


def test_reads_are_audited_and_reingestion_does_not_reopen(tmp_path) -> None:
    store = store_with_case(tmp_path)
    store.observe(event("auth_1"))
    store.observe(event("auth_2", score=0.01))
    store.list_cases(ANALYST)
    store.case("case_auth_1", ANALYST)
    log = store.audit_log(LEAD, limit=10)
    assert [row["action"] for row in log] == ["view_case", "list_cases", "case_opened"]
    assert store.audit_log(LEAD, case_id="case_auth_1", limit=10)[0]["action"] == "view_case"


def test_retention_never_purges_recent_rows(tmp_path) -> None:
    store = store_with_case(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=MIN_AUDIT_RETENTION_DAYS + 30)).isoformat()
    with store._tx() as db:
        db.execute("""INSERT INTO case_audit (at,actor,role,action,case_id,detail)
            VALUES (?,?,?,?,?,?)""", (old, "old", "lead", "view_case", "case_auth_1", "{}"))
    with pytest.raises(ValueError, match="at least"):
        store.purge_audit(LEAD, 30)
    with pytest.raises(Forbidden):
        store.purge_audit(ANALYST, MIN_AUDIT_RETENTION_DAYS)
    assert store.purge_audit(LEAD, MIN_AUDIT_RETENTION_DAYS) == 1
    actions = [row["action"] for row in store.audit_log(LEAD)]
    assert "case_opened" in actions and "purge_audit" in actions


def test_existing_sqlite_file_gains_new_columns(tmp_path) -> None:
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE cases (case_id TEXT PRIMARY KEY, auth_id TEXT UNIQUE NOT NULL,
            card_id TEXT NOT NULL, merchant_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
            draft TEXT, analyst TEXT, analyst_note TEXT, created_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO cases (case_id,auth_id,card_id,merchant_id,created_at)
            VALUES ('case_old','auth_old','card','shop','2026-10-01T00:00:00Z')""")
    store = CaseStore(path)
    store.save_draft("case_old", DRAFT, LEAD)
    assert store.case("case_old")["drafted_by"] == "lee"


def test_store_needs_exactly_one_backend(tmp_path) -> None:
    with pytest.raises(ValueError):
        CaseStore()
    with pytest.raises(ValueError):
        CaseStore(tmp_path / "x.sqlite", dsn="postgresql://unused")
