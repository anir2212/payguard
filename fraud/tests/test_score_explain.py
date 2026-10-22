"""Explaining only declines must not change any declined result (needs model artifacts)."""

from __future__ import annotations

import pytest
from test_score_threads import ARTIFACTS, EmptyHistory, request

from payguard_fraud.score_api import Scorer, ScoreRequest

pytestmark = pytest.mark.skipif(not (ARTIFACTS / "lightgbm.onnx").exists(),
                                reason="model artifacts are generated locally, not in Git")


def everyday(index: int) -> ScoreRequest:
    """An ordinary grocery purchase that scores far below the threshold."""
    return ScoreRequest.model_validate({
        "authId": f"auth_e_{index}", "cardId": "card_e", "merchantId": "m1", "mcc": "5411",
        "amountMinor": 4500 + index, "deviceId": "dev_e", "timestamp": "2026-10-22T03:00:00Z"})


def test_declines_policy_keeps_codes_for_every_decline() -> None:
    always = Scorer(ARTIFACTS, EmptyHistory(), threads=1)
    declines = Scorer(ARTIFACTS, EmptyHistory(), threads=1, explain="declines")
    seen = set()
    for scored in [request(index) for index in range(6)] + [everyday(index) for index in range(6)]:
        a, b = always.score(scored), declines.score(scored)
        assert (a["score"], a["wouldDecline"]) == (b["score"], b["wouldDecline"])
        assert b["reasonCodes"] == (a["reasonCodes"] if a["wouldDecline"] else [])
        seen.add(a["wouldDecline"])
    assert seen == {True, False}, "the requests must cover both sides of the threshold"


def test_unknown_policy_is_rejected() -> None:
    with pytest.raises(ValueError):
        Scorer(ARTIFACTS, EmptyHistory(), explain="sometimes")
