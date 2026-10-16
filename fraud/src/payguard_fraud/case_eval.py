"""Evaluate the investigation graph on 40 labelled synthetic fixtures."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from payguard_fraud.case_agent import CaseRepository, build_graph, fixture_cases


def evaluate(model: str | None, output: Path) -> dict:
    cases = fixture_cases()
    graph = build_graph(CaseRepository(cases), model=model)
    rows = []
    start = time.monotonic()
    for case in cases:
        result = graph.invoke({"case_id": case.case_id})["result"]
        valid_ids = {row["transactionId"] for row in case.transactions} | set(case.similar_cases)
        cited_ids = [id_ for claim in result["claims"] for id_ in claim["citations"]]
        rows.append({
            "caseId": case.case_id, "expectedPattern": case.label_pattern,
            "predictedPattern": result["pattern"], "expectedAction": case.label_action,
            "predictedAction": result["recommendedAction"],
            "draftSource": result["draftSource"],
            "hallucinatedIds": [id_ for id_ in cited_ids if id_ not in valid_ids],
            "approvalStatus": result["approvalStatus"],
        })
    report = {
        "fixture": "40 generated synthetic cases from six hand-built templates",
        "modelRequested": model,
        "cases": len(rows), "elapsedSeconds": round(time.monotonic() - start, 3),
        "patternAccuracy": sum(row["expectedPattern"] == row["predictedPattern"]
                               for row in rows) / len(rows),
        "actionAgreement": sum(row["expectedAction"] == row["predictedAction"]
                               for row in rows) / len(rows),
        "hallucinatedIdRate": sum(bool(row["hallucinatedIds"]) for row in rows) / len(rows),
        "draftSources": dict(Counter(row["draftSource"] for row in rows)),
        "allPendingAnalyst": all(row["approvalStatus"] == "PENDING_ANALYST" for row in rows),
        "rows": rows,
        "limit": "Template cases and rule fallback are easier than live analyst investigations; "
                 "this does not measure real-world fraud accuracy or analyst preference.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen2.5:0.5b")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("bench/case_eval_results.json"))
    args = parser.parse_args()
    result = evaluate(None if args.no_model else args.model, args.output)
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}, indent=2))


if __name__ == "__main__":
    main()
