# PayGuard

A synthetic payment authorization and fraud lab.

A payment-authorization lab with a Kotlin ledger/API, Python ONNX scoring service, Kafka/Redis features, case workflow, and a React operator view.

## Why this project

Payment decisions have to balance correctness, fraud signals, and latency under retries and concurrent requests. This synthetic system makes the ledger and scoring boundaries visible so each tradeoff can be tested locally.

## Run locally

You need Docker Desktop, Java 17, Python 3.12, uv, Node.js.

```sh
make up
make simulate
./gradlew test
make down
```

The [API contract](src/main/resources/openapi.yaml) documents the service boundary. Run the scorer checks from `fraud/` with `uv sync --extra test && uv run pytest -q`; the UI lives in `frontend/`. [Results](RESULTS.md) includes the measured local throughput and model fixtures.

## Repository map

`src/` contains the Kotlin API; `fraud/` contains scoring; `frontend/` is the operator UI; `bench/` holds load and model evidence.

## Scope

All cards, merchants, network responses, and cases are synthetic. Rules-only load figures and model scores are not a live card-network benchmark.
