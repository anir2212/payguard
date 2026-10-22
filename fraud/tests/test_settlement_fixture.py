"""The synthetic settlement fixture is reproducible and its scorer reports denominators."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from payguard_fraud.settlement_fixture import CLEAN_STATUSES, NETWORK_HEADER, generate, score


def _labels(path: Path) -> list[dict]:
    with (path / "labels.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def _perfect_predictions(path: Path) -> dict:
    """Harness-shaped output that agrees with every label (no item for unlabelled rows)."""
    output = {"fixtureId": "test", "settlementDate": "2026-01-15", "networkRows": [],
              "missingNetwork": [], "journal": []}
    for label in _labels(path):
        if label["check"] == "network_row":
            output["networkRows"].append({"networkRow": int(label["network_row"]),
                                          "captureId": label["capture_id"],
                                          "status": label["expected_status"]})
        elif label["check"] == "missing_network":
            output["missingNetwork"].append({"captureId": label["capture_id"],
                                             "status": "MISSING_NETWORK"})
        else:
            output["journal"].append({"captureId": label["capture_id"],
                                      "status": label["expected_status"]})
    return output


def test_generation_is_seeded_and_keeps_labels_out_of_reconciler_input(tmp_path) -> None:
    first = generate(tmp_path / "a", seed=7)
    second = generate(tmp_path / "b", seed=7)
    other = generate(tmp_path / "c", seed=8)
    assert first["files"] == second["files"]
    assert first["files"]["network.csv"] != other["files"]["network.csv"]
    assert first["synthetic"] is True and first["seed"] == 7
    network = (tmp_path / "a" / "network.csv").read_text().splitlines()
    assert network[0] == NETWORK_HEADER
    assert all(len(row.split(",")) == 3 for row in network[1:])
    plan_header = (tmp_path / "a" / "ledger_plan.csv").read_text().splitlines()[0]
    assert "status" not in plan_header and "case_type" not in plan_header
    labels = _labels(tmp_path / "a")
    statuses = {label["expected_status"] for label in labels}
    assert statuses >= {"MATCHED", "AMOUNT_MISMATCH", "DUPLICATE", "MISSING_LEDGER",
                        "MISSING_NETWORK", "JOURNAL_POSTED", "MISSING_JOURNAL",
                        "DUPLICATE_JOURNAL", "WRONG_CURRENCY_JOURNAL", "JOURNAL_AMOUNT_MISMATCH"}
    assert any(s in CLEAN_STATUSES for s in statuses)
    network_labels = [label for label in labels if label["check"] == "network_row"]
    assert [int(label["network_row"]) for label in network_labels] == list(
        range(1, len(network)))
    assert [label["capture_id"] for label in network_labels] == [
        row.split(",")[0] for row in network[1:]]
    # A capture missing from both journal and network file has a journal label only.
    both_omitted = {label["capture_id"] for label in labels
                    if label["case_type"] == "journal_missing_network_omitted"}
    assert len(both_omitted) == 3
    assert not any(row.split(",")[0] in both_omitted for row in network[1:])


def test_scorer_counts_classes_errors_and_unexpected_items(tmp_path) -> None:
    generate(tmp_path, seed=11)
    predictions = _perfect_predictions(tmp_path)
    (tmp_path / "perfect.json").write_text(json.dumps(predictions))
    perfect = score(tmp_path / "labels.csv", tmp_path / "perfect.json")
    assert perfect["correct"] == perfect["labels"]
    assert perfect["errors"] == [] and perfect["unexpected"] == 0
    assert perfect["positives"]["exactClass"] == perfect["positives"]["labelled"]
    assert perfect["negatives"]["falseDiscrepancy"] == 0
    assert perfect["perClass"]["MISSING_JOURNAL"] == {
        "labelled": 6, "correct": 6, "predicted": 6, "confusedWith": {}}

    # One missing journal is reported as posted, one clean journal goes missing, and an
    # unlabelled network-side item appears for a capture both sources omitted.
    missing = next(item for item in predictions["journal"] if item["status"] == "MISSING_JOURNAL")
    missing["status"] = "JOURNAL_POSTED"
    clean = next(item for item in predictions["journal"] if item["status"] == "JOURNAL_POSTED"
                 and item is not missing)
    predictions["journal"].remove(clean)
    predictions["missingNetwork"].append({"captureId": missing["captureId"],
                                          "status": "MISSING_NETWORK"})
    (tmp_path / "flawed.json").write_text(json.dumps(predictions))
    flawed = score(tmp_path / "labels.csv", tmp_path / "flawed.json")
    assert flawed["correct"] == flawed["labels"] - 2
    assert flawed["perClass"]["MISSING_JOURNAL"]["correct"] == 5
    assert flawed["perClass"]["MISSING_JOURNAL"]["confusedWith"] == {"JOURNAL_POSTED": 1}
    assert flawed["perClass"]["JOURNAL_POSTED"]["confusedWith"] == {"NO_ITEM": 1}
    assert flawed["positives"]["flaggedAsDiscrepancy"] == flawed["positives"]["labelled"] - 1
    assert flawed["negatives"]["noItem"] == 1
    assert flawed["negatives"]["falseDiscrepancy"] == 0
    assert flawed["unexpected"] == 1
    assert flawed["unexpectedPredictions"][0]["predicted"] == "MISSING_NETWORK"
