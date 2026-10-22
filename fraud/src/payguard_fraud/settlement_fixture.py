"""Synthetic settlement fixture with labels kept apart from the reconciler input.

``generate`` writes four files from its own seed. The Kotlin harness reads only
``ledger_plan.csv`` (issuer-side captures and injected journal faults) and
``network.csv`` (the reconciler's CSV input). ``labels.csv`` holds the expected
status of every network row, every capture omitted by the network, and every
capture's journal audit; the reconciler never receives it. ``score`` compares the
harness output with those labels and reports per-class counts and denominators.

Everything here is invented. Labels follow by construction from the case
specification below, so agreement shows that the reconciler implements the
documented classification on inputs it did not generate. It is not evidence
from a real card network.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

FIXTURE_ID = "payguard-settlement-synthetic-v1"
DEFAULT_SEED = 20261020
DEFAULT_DATE = "2026-01-15"
MERCHANTS = ("m_bakery", "m_transit", "m_books", "m_grocer", "m_cloud")
CLEAN_STATUSES = {"MATCHED", "JOURNAL_POSTED"}
NO_ITEM = "NO_ITEM"

# case type -> (count, journal fault, network behaviour, network label, journal label)
# Network behaviour: booked = amount on capture record, posted = journal amount,
# omit = no row, duplicate = two identical booked rows, amount/currency = altered row.
CASES: dict[str, tuple[int, str, str, str | None, str | None]] = {
    "clean_match": (20, "none", "booked", "MATCHED", "JOURNAL_POSTED"),
    "network_amount_diff": (4, "none", "amount", "AMOUNT_MISMATCH", "JOURNAL_POSTED"),
    "network_currency_diff": (3, "none", "currency", "AMOUNT_MISMATCH", "JOURNAL_POSTED"),
    "network_duplicate_row": (3, "none", "duplicate", "DUPLICATE", "JOURNAL_POSTED"),
    "network_omitted": (3, "none", "omit", "MISSING_NETWORK", "JOURNAL_POSTED"),
    "network_unknown_capture": (3, "no_capture", "booked", "MISSING_LEDGER", None),
    "journal_missing_network_present": (3, "missing", "booked", "MISSING_LEDGER",
                                        "MISSING_JOURNAL"),
    "journal_missing_network_omitted": (3, "missing", "omit", None, "MISSING_JOURNAL"),
    "journal_duplicate": (3, "duplicate", "booked", "AMOUNT_MISMATCH", "DUPLICATE_JOURNAL"),
    "journal_wrong_currency": (3, "wrong_currency", "booked", "MISSING_LEDGER",
                               "WRONG_CURRENCY_JOURNAL"),
    "journal_amount_off_network_booked": (3, "amount_off", "booked", "AMOUNT_MISMATCH",
                                          "JOURNAL_AMOUNT_MISMATCH"),
    "journal_amount_off_network_posted": (3, "amount_off", "posted", "MATCHED",
                                          "JOURNAL_AMOUNT_MISMATCH"),
}

LEDGER_FIELDS = ["capture_id", "merchant_id", "amount_minor", "journal_fault",
                 "journal_amount_minor"]
NETWORK_HEADER = "capture_id,amount_minor,currency"
LABEL_FIELDS = ["check", "network_row", "capture_id", "expected_status", "case_type"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(out_dir: Path, seed: int = DEFAULT_SEED, settlement_date: str = DEFAULT_DATE) -> dict:
    """Write ledger_plan.csv, network.csv, labels.csv and provenance.json; return provenance."""
    rng = random.Random(seed)
    cases = []
    for case_type, (count, fault, network, network_label, journal_label) in CASES.items():
        for _ in range(count):
            amount = rng.randint(100, 20_000)
            journal_amount = amount
            if fault == "amount_off":
                journal_amount = amount - rng.randint(1, min(500, amount - 1))
            altered = amount + rng.choice((-1, 1)) * rng.randint(1, 500)
            cases.append({
                "case_type": case_type, "capture_id": f"cap_fx_{rng.getrandbits(48):012x}",
                "merchant_id": rng.choice(MERCHANTS), "amount": amount,
                "journal_amount": journal_amount, "altered": max(1, altered), "fault": fault,
                "network": network, "network_label": network_label,
                "journal_label": journal_label,
            })
    rng.shuffle(cases)

    network_rows: list[tuple[str, int, str, dict]] = []
    for case in cases:
        if case["network"] == "omit":
            continue
        amount, currency = case["amount"], "SGD"
        if case["network"] == "posted":
            amount = case["journal_amount"]
        elif case["network"] == "amount":
            amount = case["altered"]
        elif case["network"] == "currency":
            currency = "USD"
        copies = 2 if case["network"] == "duplicate" else 1
        network_rows += [(case["capture_id"], amount, currency, case)] * copies
    rng.shuffle(network_rows)

    labels = []
    seen: set[str] = set()
    for row_number, (capture_id, _, _, case) in enumerate(network_rows, start=1):
        status = case["network_label"]
        if case["network"] == "duplicate":
            status = "DUPLICATE" if capture_id in seen else "MATCHED"
        seen.add(capture_id)
        labels.append({"check": "network_row", "network_row": row_number,
                       "capture_id": capture_id, "expected_status": status,
                       "case_type": case["case_type"]})
    for case in cases:
        if case["network_label"] == "MISSING_NETWORK":
            labels.append({"check": "missing_network", "network_row": "",
                           "capture_id": case["capture_id"], "expected_status": "MISSING_NETWORK",
                           "case_type": case["case_type"]})
        if case["journal_label"] is not None:
            labels.append({"check": "journal", "network_row": "",
                           "capture_id": case["capture_id"],
                           "expected_status": case["journal_label"],
                           "case_type": case["case_type"]})

    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "ledger_plan.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, LEDGER_FIELDS, lineterminator="\n")
        writer.writeheader()
        for case in cases:
            if case["fault"] == "no_capture":
                continue
            writer.writerow({
                "capture_id": case["capture_id"], "merchant_id": case["merchant_id"],
                "amount_minor": case["amount"], "journal_fault": case["fault"],
                "journal_amount_minor": "" if case["fault"] == "missing"
                else case["journal_amount"],
            })
    with (out_dir / "network.csv").open("w") as handle:
        handle.write(NETWORK_HEADER + "\n")
        for capture_id, amount, currency, _ in network_rows:
            handle.write(f"{capture_id},{amount},{currency}\n")
    with (out_dir / "labels.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, LABEL_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(labels)

    status_counts = Counter(label["expected_status"] for label in labels)
    provenance = {
        "fixtureId": FIXTURE_ID,
        "synthetic": True,
        "statement": "Invented captures and network rows. Not real card-network data and not "
                     "independent real-world evidence. Labels follow by construction from the "
                     "case specification in payguard_fraud/settlement_fixture.py and are never "
                     "passed to the reconciler.",
        "generator": "python -m payguard_fraud.settlement_fixture generate",
        "generatorSha256": _sha256(Path(__file__)),
        "seed": seed,
        "settlementDate": settlement_date,
        "billingCurrency": "SGD",
        "independence": "Network rows are written from the generator's own case table, not from "
                        "PayGuard's /v1/settlements/sample endpoint or any existing ledger.",
        "caseCounts": {case_type: spec[0] for case_type, spec in CASES.items()},
        "ledgerCaptures": sum(1 for case in cases if case["fault"] != "no_capture"),
        "networkRows": len(network_rows),
        "labels": len(labels),
        "labelStatusCounts": dict(sorted(status_counts.items())),
        "files": {name: _sha256(out_dir / name)
                  for name in ("ledger_plan.csv", "network.csv", "labels.csv")},
    }
    (out_dir / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return provenance


def _label_key(label: dict) -> tuple[str, str]:
    if label["check"] == "network_row":
        return "network_row", str(label["network_row"])
    return label["check"], label["capture_id"]


def _prediction_map(predictions: dict) -> dict[tuple[str, str], tuple[str, str]]:
    found: dict[tuple[str, str], tuple[str, str]] = {}
    for item in predictions["networkRows"]:
        found[("network_row", str(item["networkRow"]))] = (item["captureId"], item["status"])
    for item in predictions["missingNetwork"]:
        found[("missing_network", item["captureId"])] = (item["captureId"], item["status"])
    for item in predictions["journal"]:
        found[("journal", item["captureId"])] = (item["captureId"], item["status"])
    return found


def score(labels_path: Path, predictions_path: Path) -> dict:
    """Compare harness output with external labels; every rate carries its denominator."""
    with labels_path.open(newline="") as handle:
        labels = list(csv.DictReader(handle))
    predictions = json.loads(predictions_path.read_text())
    found = _prediction_map(predictions)

    confusion: dict[str, Counter] = defaultdict(Counter)
    per_check: dict[str, Counter] = defaultdict(Counter)
    errors = []
    outcomes: list[tuple[str, str]] = []
    for label in labels:
        key = _label_key(label)
        capture_id, predicted = found.pop(key, (label["capture_id"], NO_ITEM))
        if capture_id != label["capture_id"]:
            predicted = f"WRONG_CAPTURE:{predicted}"
        expected = label["expected_status"]
        outcomes.append((expected, predicted))
        confusion[expected][predicted] += 1
        per_check[label["check"]]["labelled"] += 1
        per_check[label["check"]]["correct"] += predicted == expected
        if predicted != expected:
            errors.append({"check": label["check"], "networkRow": label["network_row"] or None,
                           "captureId": label["capture_id"], "expected": expected,
                           "predicted": predicted, "caseType": label["case_type"]})
    unexpected = [{"check": key[0], "key": key[1], "captureId": capture_id, "predicted": status}
                  for key, (capture_id, status) in sorted(found.items())]

    per_class = {}
    predicted_totals: Counter = Counter()
    for row in confusion.values():
        for predicted, count in row.items():
            predicted_totals[predicted] += count
    for item in unexpected:
        predicted_totals[item["predicted"]] += 1
    for expected in sorted(set(confusion) | set(predicted_totals) - {NO_ITEM}):
        row = confusion.get(expected, Counter())
        per_class[expected] = {
            "labelled": sum(row.values()),
            "correct": row.get(expected, 0),
            "predicted": predicted_totals.get(expected, 0),
            "confusedWith": {k: v for k, v in sorted(row.items()) if k != expected},
        }

    positives = [(expected, got) for expected, got in outcomes if expected not in CLEAN_STATUSES]
    negatives = [(expected, got) for expected, got in outcomes if expected in CLEAN_STATUSES]
    flagged_positive = sum(got not in CLEAN_STATUSES | {NO_ITEM} for _, got in positives)
    exact_positive = sum(got == expected for expected, got in positives)
    false_alarms = sum(got not in CLEAN_STATUSES | {NO_ITEM} for _, got in negatives)
    return {
        "fixtureId": predictions.get("fixtureId"),
        "synthetic": True,
        "labelSource": "generated by construction from the fixture case specification; "
                       "not human labels and not a real network feed",
        "labels": len(labels),
        "correct": sum(c["correct"] for c in per_check.values()),
        "perCheck": {k: dict(v) for k, v in sorted(per_check.items())},
        "positives": {"labelled": len(positives), "flaggedAsDiscrepancy": flagged_positive,
                      "exactClass": exact_positive,
                      "noItem": sum(got == NO_ITEM for _, got in positives)},
        "negatives": {"labelled": len(negatives), "falseDiscrepancy": false_alarms,
                      "noItem": sum(got == NO_ITEM for _, got in negatives)},
        "perClass": per_class,
        "unexpected": len(unexpected),
        "unexpectedPredictions": unexpected,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate")
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--seed", type=int, default=DEFAULT_SEED)
    gen.add_argument("--date", default=DEFAULT_DATE)
    sc = commands.add_parser("score")
    sc.add_argument("--labels", type=Path, required=True)
    sc.add_argument("--predictions", type=Path, required=True)
    sc.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "generate":
        result = generate(args.out, args.seed, args.date)
    else:
        result = score(args.labels, args.predictions)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if not isinstance(v, (dict, list))}))


if __name__ == "__main__":
    main()
