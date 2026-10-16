"""Threshold selection on validation only and honest test-set fraud metrics."""

from __future__ import annotations

import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss


def cost_cents(labels, scores, amounts_minor, threshold: float,
               review_minor: int = 200, false_decline_minor: int = 500) -> int:
    """Missed fraud loss + review cost for flags + friction for false flags."""
    y = np.asarray(labels, dtype=bool)
    flagged = np.asarray(scores) >= threshold
    amounts = np.asarray(amounts_minor, dtype=np.int64)
    return int(amounts[y & ~flagged].sum() + review_minor * flagged.sum()
               + false_decline_minor * (flagged & ~y).sum())


def best_threshold(labels, scores, amounts_minor) -> float:
    candidates = np.unique(np.concatenate(([0.0, 1.0],
        np.quantile(scores, np.linspace(0, 1, 501)))))
    return float(min(candidates, key=lambda threshold:
                     cost_cents(labels, scores, amounts_minor, threshold)))


def threshold_for_fpr(labels, scores, target_fpr: float) -> float:
    negatives = np.asarray(scores)[np.asarray(labels) == 0]
    if len(negatives) == 0:
        raise ValueError("Validation set has no legitimate transactions")
    return float(np.quantile(negatives, 1 - target_fpr, method="higher"))


def evaluate(labels, scores, amounts_minor, cost_threshold: float,
             fpr_thresholds: dict[str, float]) -> dict:
    y = np.asarray(labels, dtype=int)
    p = np.asarray(scores, dtype=float)
    amounts = np.asarray(amounts_minor, dtype=np.int64)
    if len(y) != len(p) or len(y) != len(amounts) or len(np.unique(y)) != 2:
        raise ValueError("Need same-length labels, scores, amounts, and both classes")
    flagged = p >= cost_threshold
    positives = y == 1
    negatives = ~positives
    fraud_amount = amounts[positives].sum()
    report = {
        "rows": int(len(y)),
        "frauds": int(positives.sum()),
        "pr_auc": float(average_precision_score(y, p)),
        "brier": float(brier_score_loss(y, p)),
        "cost_threshold": float(cost_threshold),
        "cost_minor": cost_cents(y, p, amounts, cost_threshold),
        "recall_at_cost_threshold": float((flagged & positives).sum() / positives.sum()),
        "fpr_at_cost_threshold": float((flagged & negatives).sum() / negatives.sum()),
        "amount_weighted_recall": float(amounts[flagged & positives].sum() / fraud_amount)
        if fraud_amount else 0.0,
    }
    for label, threshold in fpr_thresholds.items():
        chosen = p >= threshold
        report[f"recall_at_{label}"] = float((chosen & positives).sum() / positives.sum())
        report[f"actual_fpr_at_{label}"] = float((chosen & negatives).sum() / negatives.sum())
        report[f"amount_weighted_recall_at_{label}"] = (
            float(amounts[chosen & positives].sum() / fraud_amount) if fraud_amount else 0.0
        )
    return report
