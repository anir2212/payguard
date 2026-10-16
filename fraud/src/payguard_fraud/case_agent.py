"""Read-only LangGraph investigation flow with cited, review-only drafts.

The fixture repository is deliberately synthetic. Swap it for a read-only case
store when integrating with the authorization stream; no tool mutates cards.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

PATTERNS = ("card_testing", "account_takeover", "merchant_collusion",
            "refund_abuse", "velocity_burst", "benign", "unclassified")
ACTIONS = ("freeze_card", "contact_customer", "no_action")
SIGNALS = {
    "card_testing": ["small_auths_1m", "count_1m"],
    "account_takeover": ["home_distance_km", "device_novel"],
    "merchant_collusion": ["merchant_risk", "linked_cards"],
    "refund_abuse": ["prior_refund_ratio", "repeated_refunds"],
    "velocity_burst": ["count_1m", "amount_z"],
    "benign": ["known_device", "normal_amount"],
    "unclassified": [],
}
EXPECTED_ACTION = {
    "card_testing": "freeze_card", "account_takeover": "contact_customer",
    "merchant_collusion": "freeze_card", "refund_abuse": "contact_customer",
    "velocity_burst": "freeze_card", "benign": "no_action",
    "unclassified": "contact_customer",
}


@dataclass(frozen=True)
class Case:
    case_id: str
    card_id: str
    merchant_id: str
    transactions: tuple[dict[str, Any], ...]
    merchant_profile: dict[str, Any]
    linked_cards: tuple[str, ...]
    reason_codes: tuple[str, ...]
    similar_cases: tuple[str, ...]
    label_pattern: str
    label_action: str


class CaseRepository:
    """Only explicit read methods are exposed to the investigation graph."""

    def __init__(self, cases: list[Case]):
        self.cases = {case.case_id: case for case in cases}

    def get_case(self, case_id: str) -> Case:
        return self.cases[case_id]

    def get_card_history(self, case_id: str) -> list[dict[str, Any]]:
        return list(self.get_case(case_id).transactions)

    def get_merchant_profile(self, case_id: str) -> dict[str, Any]:
        return dict(self.get_case(case_id).merchant_profile)

    def get_linked_cards(self, case_id: str) -> list[str]:
        return list(self.get_case(case_id).linked_cards)

    def get_reason_codes(self, case_id: str) -> list[str]:
        return list(self.get_case(case_id).reason_codes)

    def get_similar_cases(self, case_id: str) -> list[str]:
        return list(self.get_case(case_id).similar_cases)


def fixture_cases() -> list[Case]:
    """Thirty fraud cases (six per pattern) and ten ordinary cases."""
    patterns = [*sum(([pattern] * 6 for pattern in PATTERNS[:-2]), []), *(["benign"] * 10)]
    cases = []
    for index, pattern in enumerate(patterns):
        case_id = f"case_{index:03d}"
        card_id = f"card_case_{index:03d}"
        merchant_id = f"merchant_case_{index:03d}"
        event_count = 6 if pattern in {"card_testing", "velocity_burst"} else 3
        transactions = tuple({
            "transactionId": f"txn_case_{index:03d}_{event}",
            "cardId": card_id,
            "merchantId": merchant_id,
            "amountMinor": (100 + event * 5) if pattern == "card_testing" else
                           (80_000 if pattern == "account_takeover" else 3_000 + event * 100),
            "deviceId": "new_device" if pattern in {"card_testing", "account_takeover"}
                        else "known_device",
            "geo": "distant" if pattern == "account_takeover" else "home",
        } for event in range(event_count))
        linked = (f"card_linked_{index:03d}",) if pattern == "merchant_collusion" else ()
        cases.append(Case(case_id, card_id, merchant_id, transactions, {
            "merchantId": merchant_id, "risk": 0.9 if pattern == "merchant_collusion" else 0.1,
        }, linked, tuple(SIGNALS[pattern]),
            (f"case_similar_{index:03d}",) if pattern != "benign" else (),
            pattern, EXPECTED_ACTION[pattern]))
    return cases


class State(TypedDict, total=False):
    case_id: str
    evidence: dict[str, Any]
    draft: dict[str, Any]
    result: dict[str, Any]


def observed_signals(evidence: dict[str, Any]) -> list[str]:
    """Add verifiable event-count signals to the model's explanation codes."""
    codes = set(evidence["reasonCodes"])
    history = evidence["cardHistory"]
    timestamped = [row for row in history if "eventAt" in row]
    if timestamped:
        latest = max(datetime.fromisoformat(row["eventAt"]) for row in timestamped)
        recent = [row for row in timestamped if
                  datetime.fromisoformat(row["eventAt"]) >= latest - timedelta(minutes=1)]
        if len(recent) >= 3:
            codes.add("count_1m")
            if sum(row["amountMinor"] < 300 for row in recent) >= 3:
                codes.add("small_auths_1m")
    return sorted(codes)


def _fallback(evidence: dict[str, Any]) -> dict[str, Any]:
    codes = set(evidence["observedSignals"])
    pattern = next((name for name in PATTERNS if set(SIGNALS[name]) <= codes), "unclassified")
    ids = [row["transactionId"] for row in evidence["cardHistory"]]
    return {
        "pattern": pattern, "recommendedAction": EXPECTED_ACTION[pattern],
        "confidence": 0.5 if pattern == "unclassified" else 0.8,
        "claims": [{
            "text": f"Observed {pattern.replace('_', ' ')} signals in recent activity.",
            "citations": ids,
        }],
    }


def _ollama_draft(evidence: dict[str, Any], model: str) -> dict[str, Any]:
    # The model classifies. Grounded prose and citations are assembled from
    # retrieved evidence so the model cannot invent transaction IDs or amounts.
    compact = {
        "reasonCodes": evidence["reasonCodes"],
        "observedSignals": evidence["observedSignals"],
        "merchantRisk": evidence["merchantProfile"]["risk"],
        "linkedCardCount": len(evidence["linkedCards"]),
        "transactionCount": len(evidence["cardHistory"]),
        "amountsMinor": [row["amountMinor"] for row in evidence["cardHistory"]],
        "deviceIds": list({row["deviceId"] for row in evidence["cardHistory"]}),
        "geographies": list({row["geo"] for row in evidence["cardHistory"]}),
    }
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "pattern": {"type": "string", "enum": list(PATTERNS)},
            "recommendedAction": {"type": "string", "enum": list(ACTIONS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["pattern", "recommendedAction", "confidence"],
    }
    prompt = (
        "Classify this synthetic card case. Signal mapping: "
        "small_auths_1m+count_1m=card_testing; "
        "home_distance_km+device_novel=account_takeover; "
        "merchant_risk+linked_cards=merchant_collusion; "
        "prior_refund_ratio+repeated_refunds=refund_abuse; "
        "count_1m+amount_z=velocity_burst; known_device+normal_amount=benign; "
        "otherwise=unclassified. "
        "Actions: card_testing/merchant_collusion/velocity_burst=freeze_card; "
        "account_takeover/refund_abuse/unclassified=contact_customer; benign=no_action. "
        "Return only JSON matching this schema: " + json.dumps(schema) +
        "\nEvidence: " + json.dumps(compact)
    )
    endpoint = os.getenv("PAYGUARD_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
    with httpx.Client(timeout=45.0) as client:
        response = client.post(endpoint, json={
            "model": model, "prompt": prompt, "format": schema, "stream": False,
            "options": {"temperature": 0},
        })
        response.raise_for_status()
        decision = json.loads(response.json()["response"])
        if decision.get("recommendedAction") != EXPECTED_ACTION.get(decision.get("pattern")):
            raise ValueError("model action is inconsistent with its pattern")
        if not set(SIGNALS[decision["pattern"]]) <= set(evidence["observedSignals"]):
            raise ValueError("model pattern is unsupported by observed signals")
        ids = [row["transactionId"] for row in evidence["cardHistory"]]
        return {**decision, "claims": [{
            "text": f"Observed {decision['pattern'].replace('_', ' ')} signals in recent activity.",
            "citations": ids,
        }]}


def validate_draft(draft: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Reject unknown IDs, unsupported numeric text, and uncited claims."""
    if draft.get("pattern") not in PATTERNS or draft.get("recommendedAction") not in ACTIONS:
        raise ValueError("invalid pattern or action")
    confidence = draft.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("invalid confidence")
    valid_ids = {row["transactionId"] for row in evidence["cardHistory"]}
    valid_ids.update(evidence["similarCases"])
    claims = draft.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("draft needs cited claims")
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str):
            raise ValueError("invalid claim")
        citations = claim.get("citations")
        if not isinstance(citations, list) or not citations or not all(
            isinstance(citation, str) and citation in valid_ids for citation in citations
        ):
            raise ValueError("claim has missing or invented citation")
        if re.search(r"\d", claim["text"]):
            raise ValueError("numeric or ID claim text must be expressed through citations")
    return draft


def build_graph(repository: CaseRepository, *, model: str | None = None):
    """Compile retrieval → draft → evidence-check graph; return review-only output."""

    def retrieve(state: State) -> State:
        case_id = state["case_id"]
        evidence = {
            "caseId": case_id,
            "cardHistory": repository.get_card_history(case_id),
            "merchantProfile": repository.get_merchant_profile(case_id),
            "linkedCards": repository.get_linked_cards(case_id),
            "reasonCodes": repository.get_reason_codes(case_id),
            "similarCases": repository.get_similar_cases(case_id),
        }
        evidence["observedSignals"] = observed_signals(evidence)
        return {"evidence": evidence}

    def draft(state: State) -> State:
        evidence = state["evidence"]
        if model:
            try:
                candidate = _ollama_draft(evidence, model)
                validate_draft(candidate, evidence)
                return {"draft": {**candidate, "draftSource": "ollama"}}
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass
        return {"draft": {**_fallback(evidence), "draftSource": "rules_fallback"}}

    def verify(state: State) -> State:
        candidate = validate_draft(state["draft"], state["evidence"])
        return {"result": {
            "caseId": state["case_id"], **candidate, "approvalStatus": "PENDING_ANALYST",
            "summary": " ".join(claim["text"] for claim in candidate["claims"]),
        }}

    graph = StateGraph(State)
    graph.add_node("retrieve", retrieve)
    graph.add_node("draft", draft)
    graph.add_node("verify", verify)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "draft")
    graph.add_edge("draft", "verify")
    graph.add_edge("verify", END)
    return graph.compile()


def investigate(case_id: str, *, model: str | None = None) -> dict[str, Any]:
    repository = CaseRepository(fixture_cases())
    return build_graph(repository, model=model).invoke({"case_id": case_id})["result"]


if __name__ == "__main__":
    print(json.dumps(investigate("case_000", model=os.getenv("PAYGUARD_CASE_MODEL")), indent=2))
