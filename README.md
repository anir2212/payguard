# PayGuard

Card authorization has to be fast, but each approval must also respect available funds, spending rules, and fraud signals. PayGuard is a local system for studying those tradeoffs. A Kotlin API manages synthetic cards and an append-only PostgreSQL ledger; a Python service scores fraud signals; Kafka and Redis carry event history; and a React console helps an analyst review cases and settlement differences.

An authorization places a hold. A later capture posts a balanced transfer. The settlement checker reads those posted journal amounts rather than trusting capture metadata. Fraud scoring has a 50 ms caller timeout and a documented rules fallback, so a slow model does not silently stall the payment path. Case recommendations cite retrieved transactions and remain subject to human review; approving a case does not freeze a card.

## What has been checked

- The 15 October local Gradle run passed 13 tests, including PostgreSQL journal and shared company-limit checks.
- On the intentionally learnable synthetic fraud set, the later held-out LightGBM PR-AUC was 0.9574. A separate model on the OpenML benchmark reached 0.7733 PR-AUC. These datasets have different feature contracts and the values are not transfer results.
- A guarded case workflow matched 40/40 hand-built template patterns and actions without invented cited IDs. The local model produced 12 accepted drafts; 28 cases used the rules fallback.
- In a 15-second, **rules-only** load run at 500 offered authorizations per second, 7,501 completed with 6.077 ms p95 and zero authorization errors. Synchronous fraud scoring and event publication were outside that load measurement.

The [measured results](RESULTS.md) give hardware, denominators, raw artifacts, and limitations. These checks do not establish live card-network capacity or real-world fraud accuracy.

## Run locally

Docker Desktop, Java 17, Python 3.12, uv, and Node.js are used across the stack. The packaged demo needs enough free disk for the images. All exposed Compose ports bind to localhost and use synthetic tokens and demo credentials.

```sh
make up
make simulate
./gradlew test
make down
```

`make up` starts PostgreSQL, Redis, Kafka, the Kotlin API, ONNX scorer, feature and case consumers, and the console. `make simulate` issues a synthetic card and sends a short card-testing burst. The console is at <http://127.0.0.1:18119>; the API is at <http://127.0.0.1:8086>. Stop the stack with `make down` when finished. For focused scorer checks, run `uv sync --locked --extra serve --extra export --extra agent --extra test` and `.venv/bin/pytest tests` from `fraud/`.

The [OpenAPI contract](src/main/resources/openapi.yaml) describes the card and payment routes. `src/` contains the Kotlin API and ledger; `fraud/` contains model training, serving and case analysis; `frontend/` is the analyst console; `bench/` holds the raw local results. The [data provenance note](data/SOURCES.md) explains why the public benchmark and synthetic online model are evaluated separately.

## What remains

The next checks are a capture-to-journal integrity audit, settlement cases from a source independent of this ledger, and a load test of the complete fraud-enabled route with queue lag and fallback rates. An authenticated analyst identity and durable shared case store are needed before a user-facing deployment. No real cards, customer data, or network feed are used here.
