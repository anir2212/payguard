"""Bounded in-process Prometheus metrics and sample-gated score drift."""

from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path
from threading import Lock

import numpy as np


class ScoreMetrics:
    LATENCY_BOUNDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1.0)
    SCORE_BOUNDS = (0.1, 0.2, 0.4, 0.6, 0.8, 1.0)

    def __init__(self, baseline_file: Path | None = None):
        self.lock = Lock()
        self.count = 0
        self.errors = 0
        self.seconds_sum = 0.0
        self.score_sum = 0.0
        self.latency_counts = [0] * len(self.LATENCY_BOUNDS)
        self.score_counts = [0] * len(self.SCORE_BOUNDS)
        self.recent: deque[float] = deque(maxlen=1_000)
        self.baseline = (json.loads(baseline_file.read_text())
                         if baseline_file and baseline_file.exists() else None)

    def observe(self, seconds: float, score: float | None) -> None:
        with self.lock:
            self.seconds_sum += seconds
            for index, bound in enumerate(self.LATENCY_BOUNDS):
                self.latency_counts[index] += int(seconds <= bound)
            if score is None:
                self.errors += 1
            else:
                self.count += 1
                self.score_sum += score
                self.recent.append(score)
                for index, bound in enumerate(self.SCORE_BOUNDS):
                    self.score_counts[index] += int(score <= bound)

    def psi(self) -> float | None:
        if self.baseline is None or len(self.recent) < 100:
            return None
        boundaries = self.baseline["boundaries"]
        actual = np.histogram(self.recent, bins=[0, *boundaries, 1.0000001])[0]
        observed = actual / actual.sum()
        baseline = self.baseline["proportions"]
        epsilon = 1e-5
        return float(sum((p - q) * math.log((p + epsilon) / (q + epsilon))
                         for p, q in zip(observed, baseline, strict=True)))

    def render(self) -> str:
        with self.lock:
            lines = [
                "# HELP payguard_scoring_requests_total Local fraud scoring requests",
                "# TYPE payguard_scoring_requests_total counter",
                f'payguard_scoring_requests_total{{status="ok"}} {self.count}',
                f'payguard_scoring_requests_total{{status="error"}} {self.errors}',
                "# HELP payguard_scoring_duration_seconds HTTP scoring service duration",
                "# TYPE payguard_scoring_duration_seconds histogram",
            ]
            lines += [
                f'payguard_scoring_duration_seconds_bucket{{le="{bound}"}} {count}'
                for bound, count in zip(self.LATENCY_BOUNDS, self.latency_counts, strict=True)
            ]
            lines += [
                f'payguard_scoring_duration_seconds_bucket{{le="+Inf"}} {self.count + self.errors}',
                f"payguard_scoring_duration_seconds_sum {self.seconds_sum}",
                f"payguard_scoring_duration_seconds_count {self.count + self.errors}",
                "# HELP payguard_online_score Calibrated model score distribution",
                "# TYPE payguard_online_score histogram",
            ]
            lines += [f'payguard_online_score_bucket{{le="{bound}"}} {count}'
                      for bound, count in zip(self.SCORE_BOUNDS, self.score_counts, strict=True)]
            lines += [
                f'payguard_online_score_bucket{{le="+Inf"}} {self.count}',
                f"payguard_online_score_sum {self.score_sum}",
                f"payguard_online_score_count {self.count}",
            ]
            drift = self.psi()
            if drift is not None:
                lines += ["# HELP payguard_model_score_psi Population stability index",
                          "# TYPE payguard_model_score_psi gauge",
                          f"payguard_model_score_psi {drift}"]
            return "\n".join(lines) + "\n"
