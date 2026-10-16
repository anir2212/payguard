"""The investigation agent may only return cited, review-only drafts."""

from __future__ import annotations

import pytest

from payguard_fraud.case_agent import CaseRepository, build_graph, fixture_cases, validate_draft


def test_all_fixture_cases_remain_review_only() -> None:
    cases = fixture_cases()
    assert len(cases) == 40
    graph = build_graph(CaseRepository(cases))
    for case in cases:
        result = graph.invoke({"case_id": case.case_id})["result"]
        assert result["approvalStatus"] == "PENDING_ANALYST"
        assert result["pattern"] == case.label_pattern
        assert result["recommendedAction"] == case.label_action
        assert result["claims"]


def test_checker_rejects_invented_ids_and_uncited_numbers() -> None:
    case = fixture_cases()[0]
    evidence = {
        "cardHistory": list(case.transactions),
        "similarCases": list(case.similar_cases),
    }
    draft = {
        "pattern": case.label_pattern,
        "recommendedAction": case.label_action,
        "confidence": 0.8,
        "claims": [{"text": "Small authorizations occurred.",
                    "citations": [case.transactions[0]["transactionId"]]}],
    }
    assert validate_draft(draft, evidence) == draft
    draft["claims"][0]["citations"] = ["txn_invented"]
    with pytest.raises(ValueError, match="invented"):
        validate_draft(draft, evidence)
    draft["claims"][0]["citations"] = [case.transactions[0]["transactionId"]]
    draft["claims"][0]["text"] = "There were 6 tiny authorizations."
    with pytest.raises(ValueError, match="numeric"):
        validate_draft(draft, evidence)
