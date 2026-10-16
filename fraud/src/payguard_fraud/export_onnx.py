"""Export the synthetic LightGBM model and check ONNX/native probability parity."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import joblib
import numpy as np
import onnxmltools
import onnxruntime as ort
from onnxmltools.convert.common.data_types import FloatTensorType

from payguard_fraud.features import FEATURES, build_frame
from payguard_fraud.simulator import generate


def export(artifacts: Path, samples: int = 1_000) -> dict:
    model = joblib.load(artifacts / "lightgbm_model.joblib")
    calibrator = joblib.load(artifacts / "lightgbm_calibration.joblib")
    frame = build_frame(generate(max(samples, 1_000), seed=99))
    vector = frame[list(FEATURES)].iloc[:samples].to_numpy(dtype=np.float32)
    converted = onnxmltools.convert_lightgbm(
        model, initial_types=[("input", FloatTensorType([None, len(FEATURES)]))],
        target_opset=15, zipmap=False,
    )
    destination = artifacts / "lightgbm.onnx"
    destination.write_bytes(converted.SerializeToString())
    session = ort.InferenceSession(str(destination), providers=["CPUExecutionProvider"])
    output = session.run(None, {session.get_inputs()[0].name: vector})
    onnx_raw = np.asarray(output[1])[:, 1]
    native_raw = model.predict_proba(vector)[:, 1]
    maximum_raw_error = float(np.max(np.abs(onnx_raw - native_raw)))
    maximum_calibrated_error = float(np.max(np.abs(
        calibrator.predict(onnx_raw) - calibrator.predict(native_raw)
    )))
    if maximum_raw_error > 0.01 or maximum_calibrated_error > 0.01:
        raise AssertionError("ONNX export differs from native model by more than one point")
    single = vector[:1]
    timings_ms = []
    for index in range(1_100):
        began = time.perf_counter_ns()
        session.run(None, {session.get_inputs()[0].name: single})
        elapsed_ms = (time.perf_counter_ns() - began) / 1_000_000
        if index >= 100:
            timings_ms.append(elapsed_ms)
    ordered = sorted(timings_ms)
    report = {
        "samples_checked": samples,
        "max_raw_probability_error": maximum_raw_error,
        "max_calibrated_probability_error": maximum_calibrated_error,
        "single_row_onnx_inference_ms": {
            "p50": statistics.median(ordered),
            "p95": ordered[949],
            "p99": ordered[989],
        },
        "note": "In-process CPU inference only; excludes HTTP, Redis, feature lookup "
        "and Kotlin caller",
    }
    (artifacts / "onnx_results.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    print(json.dumps(export(args.artifacts), indent=2))


if __name__ == "__main__":
    main()
