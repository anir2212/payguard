"""Train time-split synthetic fraud baselines, calibrate, and record test metrics."""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

import joblib
import numpy as np
from lightgbm import LGBMClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from payguard_fraud.features import FEATURES, build_frame
from payguard_fraud.metrics import best_threshold, cost_cents, evaluate, threshold_for_fpr
from payguard_fraud.simulator import generate


def evaluate_model(model, train, validation, test, output: Path, name: str,
                   columns: list[str] | None = None) -> dict:
    columns = columns or list(FEATURES)
    model.fit(train[columns], train["label"])
    raw_validation = model.predict_proba(validation[columns])[:, 1]
    calibration = IsotonicRegression(out_of_bounds="clip")
    calibration.fit(raw_validation, validation["label"])
    val_scores = calibration.predict(raw_validation)
    test_scores = calibration.predict(model.predict_proba(test[columns])[:, 1])
    threshold = best_threshold(validation["label"], val_scores, validation["amount_minor"])
    fpr_thresholds = {
        "0_5pct_fpr": threshold_for_fpr(validation["label"], val_scores, 0.005),
        "1pct_fpr": threshold_for_fpr(validation["label"], val_scores, 0.01),
    }
    report = evaluate(test["label"], test_scores, test["amount_minor"],
                      threshold, fpr_thresholds)
    report["validation_cost_minor"] = cost_cents(
        validation["label"], val_scores, validation["amount_minor"], threshold
    )
    report["fpr_thresholds"] = fpr_thresholds
    if name == "lightgbm":
        boundaries = [0.01, 0.05, 0.1, 0.2, 0.5]
        counts = np.histogram(val_scores, bins=[0, *boundaries, 1.0000001])[0]
        baseline = {"boundaries": boundaries,
                    "proportions": (counts / counts.sum()).tolist(),
                    "validation_rows": len(validation)}
        (output / "score_baseline.json").write_text(json.dumps(baseline, indent=2))
    joblib.dump(model, output / f"{name}_model.joblib")
    joblib.dump(calibration, output / f"{name}_calibration.joblib")
    return report


def train(count: int, output: Path, seed: int = 11) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    frame = build_frame(generate(count, seed))
    start = frame["timestamp"].min()
    train_end = start + np.timedelta64(35, "D")
    validation_end = start + np.timedelta64(45, "D")
    train_rows = frame[frame.timestamp < train_end]
    validation = frame[(frame.timestamp >= train_end) & (frame.timestamp < validation_end)]
    test = frame[frame.timestamp >= validation_end]
    if min(train_rows.label.sum(), validation.label.sum(), test.label.sum()) < 10:
        raise ValueError("Need at least ten fraud examples in every chronological split")
    logistic = make_pipeline(StandardScaler(), LogisticRegression(
        max_iter=1000, class_weight="balanced", random_state=seed,
    ))
    trees = LGBMClassifier(n_estimators=250, learning_rate=0.05, num_leaves=31,
                           class_weight="balanced", random_state=seed, n_jobs=2, verbosity=-1)
    result = {
        "seed": seed,
        "requested_base_events": count,
        "actual_events": len(frame),
        "split": {"train": len(train_rows), "validation": len(validation), "test": len(test)},
        "frauds": {"train": int(train_rows.label.sum()), "validation": int(validation.label.sum()),
                   "test": int(test.label.sum())},
        "features": list(FEATURES),
        "hardware": {"platform": platform.platform(), "processor": platform.processor()},
        "logistic": evaluate_model(logistic, train_rows, validation, test, output, "logistic"),
        "lightgbm": evaluate_model(trees, train_rows, validation, test, output, "lightgbm"),
    }
    (output / "feature_names.json").write_text(json.dumps(list(FEATURES)))
    (output / "synthetic_results.json").write_text(json.dumps(result, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=60_000)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    print(json.dumps(train(args.count, args.output, args.seed), indent=2))


if __name__ == "__main__":
    main()
