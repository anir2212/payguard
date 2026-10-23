"""Opt-in PostgreSQL checks for the shared case store.

Set ``PAYGUARD_TEST_PG_DSN`` to a disposable database whose name contains
``payguard_cases_it``; the test drops and recreates the case tables there.
"""

from __future__ import annotations

import os
import threading

import pytest

from payguard_fraud.case_store import Actor, CaseStore, Forbidden

DSN = os.getenv("PAYGUARD_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="set PAYGUARD_TEST_PG_DSN to run")
LEAD_A, LEAD_B = Actor("lead_a", "lead"), Actor("lead_b", "lead")
DRAFT = {"pattern": "card_testing", "recommendedAction": "freeze_card",
         "draftSource": "rules_fallback", "claims": []}


@pytest.fixture
def store() -> CaseStore:
    import psycopg

    if "payguard_cases_it" not in DSN:
        pytest.fail("refusing to drop tables outside a payguard_cases_it database")
    with psycopg.connect(DSN, autocommit=True) as db:
        db.execute("DROP TABLE IF EXISTS case_audit, case_reviews, cases, events")
    created = CaseStore(dsn=DSN)
    for index in range(2):
        created.observe({
            "timestamp": f"2026-10-11T08:00:0{index}Z",
            "request": {"authId": f"auth_{index}", "cardId": "card_1", "merchantId": "shop",
                        "amountMinor": 100 + index, "deviceId": "dev"},
            "result": {"status": "DECLINED", "modelScore": 0.95, "modelReasonCodes": []}})
    return created


def test_shared_store_survives_reopen_and_keeps_history(store) -> None:
    store.save_draft("case_auth_0", DRAFT, Actor("ana", "analyst"))
    store.review("case_auth_0", "APPROVED", LEAD_A, "confirmed with customer")
    reopened = CaseStore(dsn=DSN)  # A second process would open it the same way.
    assert reopened.case("case_auth_0")["status"] == "APPROVED"
    assert [row["analyst"] for row in reopened.reviews("case_auth_0")] == ["lead_a"]
    assert len(reopened.get_card_history("case_auth_1")) == 2
    assert reopened.get_similar_cases("case_auth_0") == ["case_auth_1"]
    actions = [row["action"] for row in reversed(reopened.audit_log(LEAD_B))]
    assert actions[:4] == ["case_opened", "case_opened", "draft_saved", "review_recorded"]
    with pytest.raises(Forbidden):
        reopened.review("case_auth_1", "APPROVED", Actor("ana", "analyst"), "")


def test_concurrent_reviews_from_two_stores_record_exactly_one(store) -> None:
    store.save_draft("case_auth_1", DRAFT, LEAD_A)
    stores = [CaseStore(dsn=DSN), CaseStore(dsn=DSN)]
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def attempt(index: int) -> None:
        barrier.wait()
        try:
            stores[index].review("case_auth_1", ["APPROVED", "REJECTED"][index],
                                 [LEAD_A, LEAD_B][index], "race")
            outcomes.append("recorded")
        except ValueError:
            outcomes.append("refused")

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["recorded", "refused"]
    assert len(store.reviews("case_auth_1")) == 1
    assert store.case("case_auth_1")["status"] in {"APPROVED", "REJECTED"}


def test_concurrent_first_opens_create_the_schema_once(store) -> None:
    """The API and the worker open the store at the same moment on a fresh database."""
    import psycopg

    with psycopg.connect(DSN, autocommit=True) as db:
        db.execute("DROP TABLE IF EXISTS case_audit, case_reviews, cases, events")
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def open_store() -> None:
        barrier.wait()
        try:
            CaseStore(dsn=DSN)
        except BaseException as exc:  # noqa: BLE001 - record any failure for the assert
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert CaseStore(dsn=DSN).list_cases() == []
