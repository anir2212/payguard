"""Measure local HTTP scoring latency, including feature lookup and explanations."""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def main() -> None:
    url = os.getenv("PAYGUARD_SCORE_URL", "http://127.0.0.1:18086/score")
    count = int(os.getenv("PAYGUARD_BENCH_REQUESTS", "200"))
    if count < 1:
        raise ValueError("request count must be positive")
    card = f"card_bench_{uuid4().hex}"
    payload = {
        "authId": "auth_bench", "cardId": card, "merchantId": "merchant_bench",
        "mcc": "5411", "amountMinor": 1000, "deviceId": "device_bench",
    }
    body = json.dumps(payload).encode()

    def one() -> float:
        request = Request(url, body, {"Content-Type": "application/json"})
        start = time.perf_counter_ns()
        with urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError(f"unexpected HTTP {response.status}")
            json.load(response)
        return (time.perf_counter_ns() - start) / 1_000_000

    for _ in range(10):
        one()
    values = [one() for _ in range(count)]
    result = {
        "description": ("Sequential localhost HTTP POST /score, including Redis, ONNX, "
                        "calibration, and reason codes"),
        "requests": count, "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(percentile(values, 95), 3),
        "p99_ms": round(percentile(values, 99), 3),
        "max_ms": round(max(values), 3),
        "note": "Sequential warm requests only; excludes Kotlin auth path and concurrent load",
    }
    target = Path(os.getenv("PAYGUARD_SCORE_REPORT", "bench/score_http_results.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
