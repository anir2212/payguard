"""Case ingestion is idempotent; drafts and reviews are role-checked and audited."""

from __future__ import annotations

import pytest

from payguard_fraud.case_agent import build_graph
from payguard_fraud.case_store import Actor, CaseStore

LEAD = Actor("lead_1", "lead")


def test_high_score_opens_once_and_requires_review(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases.sqlite")
    payload = {
        "timestamp": "2026-10-11T08:00:00Z",
        "request": {"authId": "auth_live_1", "cardId": "card_live_1",
                    "merchantId": "shop", "amountMinor": 100,
                    "deviceId": "new", "merchantRisk": 0.1},
        "result": {"status": "DECLINED", "modelScore": 0.95,
                   "modelReasonCodes": ["small_auths_1m", "device_novel"]},
    }
    assert store.observe(payload) == "case_auth_live_1"
    assert store.observe(payload) == "case_auth_live_1"
    assert len(store.list_cases()) == 1
    assert len(store.get_card_history("case_auth_live_1")) == 1
    with pytest.raises(ValueError, match="draft absent"):
        store.review("case_auth_live_1", "APPROVED", LEAD, "")
    result = build_graph(store).invoke({"case_id": "case_auth_live_1"})["result"]
    assert result["pattern"] == "unclassified"
    assert result["recommendedAction"] == "contact_customer"
    store.save_draft("case_auth_live_1", result, LEAD)
    store.review("case_auth_live_1", "APPROVED", LEAD, "seen")
    assert store.case("case_auth_live_1")["status"] == "APPROVED"
    with pytest.raises(ValueError, match="already reviewed"):
        store.review("case_auth_live_1", "REJECTED", LEAD, "")


def test_recent_small_authorizations_ground_card_testing_pattern(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases.sqlite")
    for index in range(3):
        store.observe({
            "timestamp": f"2026-10-11T08:00:{index:02d}Z",
            "request": {"authId": f"auth_small_{index}", "cardId": "card_small",
                        "merchantId": "shop", "amountMinor": 100 + index,
                        "deviceId": "new"},
            "result": {"status": "DECLINED", "modelScore": 0.95 if index == 2 else 0.01,
                       "modelReasonCodes": []},
        })
    result = build_graph(store).invoke({"case_id": "case_auth_small_2"})["result"]
    assert result["pattern"] == "card_testing"
    assert len(result["claims"][0]["citations"]) == 3
