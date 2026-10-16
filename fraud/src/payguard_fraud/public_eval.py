"""Separate chronological benchmark on anonymized OpenML dataset 43627."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from payguard_fraud.train import evaluate_model

PUBLIC_FEATURES = [f"V{i}" for i in range(1, 29)] + ["Amount"]


def benchmark(source: Path, output: Path) -> dict:
    frame = pd.read_parquet(source)
    expected = set(PUBLIC_FEATURES) | {"Time", "Class"}
    if not expected.issubset(frame.columns):
        missing = sorted(expected - set(frame.columns))
        raise ValueError(f"Unexpected OpenML columns; missing {missing}")
    frame = frame.sort_values("Time", kind="stable").reset_index(drop=True)
    frame["label"] = frame["Class"].astype(int)
    # The source does not identify a currency. These scaled amounts are used only for
    # relative amount-weighted metrics and an illustrative cost sensitivity analysis.
    frame["amount_minor"] = (frame["Amount"].clip(lower=0) * 100).round().astype("int64")
    train_end = frame["Time"].quantile(0.60)
    validation_end = frame["Time"].quantile(0.80)
    training = frame[frame.Time < train_end]
    validation = frame[(frame.Time >= train_end) & (frame.Time < validation_end)]
    test = frame[frame.Time >= validation_end]
    if min(training.label.sum(), validation.label.sum(), test.label.sum()) < 10:
        raise ValueError("Chronological split needs at least ten fraud rows in each period")
    output.mkdir(parents=True, exist_ok=True)
    logistic = make_pipeline(StandardScaler(), LogisticRegression(
        max_iter=1000, class_weight="balanced", random_state=11,
    ))
    trees = LGBMClassifier(n_estimators=250, learning_rate=0.05, num_leaves=31,
                           class_weight="balanced", random_state=11, n_jobs=2, verbosity=-1)
    result = {
        "source": "OpenML 43627; see data/SOURCES.md",
        "note": "Different feature contract from synthetic model; no cross-dataset transfer claim",
        "split": {"train": len(training), "validation": len(validation), "test": len(test)},
        "frauds": {"train": int(training.label.sum()),
                   "validation": int(validation.label.sum()), "test": int(test.label.sum())},
        "features": PUBLIC_FEATURES,
        "logistic": evaluate_model(logistic, training, validation, test, output,
                                   "public_logistic", PUBLIC_FEATURES),
        "lightgbm": evaluate_model(trees, training, validation, test, output,
                                   "public_lightgbm", PUBLIC_FEATURES),
    }
    (output / "public_results.json").write_text(json.dumps(result, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("../data/raw/openml_43627.parquet"))
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    print(json.dumps(benchmark(args.source, args.output), indent=2))


if __name__ == "__main__":
    main()
