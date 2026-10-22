# PayGuard

Card authorization has to be fast, but each approval must also respect available funds, spending rules, and fraud signals. PayGuard is a local system for studying those tradeoffs. A Kotlin API manages synthetic cards and an append-only PostgreSQL ledger; a Python service scores fraud signals; Kafka and Redis carry event history; and a React console helps an analyst review cases and settlement differences.

An authorization places a hold. A later capture posts a balanced transfer. The settlement checker reads those posted journal amounts rather than trusting capture metadata. Fraud scoring has a 50 ms caller timeout and a documented rules fallback, so a slow model does not silently stall the payment path. Case recommendations cite retrieved transactions and remain subject to human review; approving a case does not freeze a card.

## What has been checked

- **Ledger tests.** The 22 October Gradle run passed 21 tests against real PostgreSQL, including journal, shared company-limit, capture-to-journal integrity and query-plan checks.
- **Settlement.** A capture-to-journal audit (`GET /v1/settlements/integrity`) finds captures whose journal is missing, duplicated, in the wrong currency or for the wrong amount, including a capture that both the journal and the network file omit. A separately generated synthetic settlement fixture scored 105/105 against labels the reconciler never reads.
- **Fraud models.** On the intentionally learnable synthetic fraud set, the later held-out LightGBM PR-AUC was 0.9574. A separate model on the OpenML benchmark reached 0.7733 PR-AUC. These datasets have different feature contracts and the values are not transfer results.
- **Case review.** A guarded case workflow matched 40/40 hand-built template patterns and actions without invented cited IDs; the local model produced 12 accepted drafts and 28 cases used the rules fallback. The case API takes the analyst's identity from the server (proxy headers with a shared token, or one local identity), not from the form. Only a `lead` may record a decision, every decision is kept in an append-only history, and reads, drafts and reviews are audited. A headless-Chrome review passed 10/10 checks, and an optional PostgreSQL case store recorded exactly one of two concurrent reviews.
- **Full scored path under load.** On 22 October the whole stack ran under a mixed k6 workload with scoring enforced. The first runs held 250 authorizations a second and saturated at 500. Repeated runs on fresh databases traced the 500/s ceiling to a 40 ms pause on every reused connection to a multi-worker scorer, because uvicorn left Nagle's algorithm on. With that fixed, the new default of four scorer workers at one thread each completed all 7,501 scheduled authorizations at 500/s in three of three runs (median p50 6.9 ms, p99 174 ms, under 2% rules fallback). Two missing indexes whose cost grew with the data were also found and fixed.
- **Where it saturates.** Sampled runs put the knee at about 730 authorizations a second on this 11-core laptop. At 750/s all 20 pooled connections were busy, and at 800/s the Mac had 1–2% idle CPU, shared by every service and the load generator. The stack used about 11 ms of CPU per authorization, 3.7 ms of it in the scorer. Loaded alone, the scorer needs only 1.2 ms per request (0.61 ms of it the score itself), so contention on the shared CPU roughly tripled its cost ([record](bench/full_path_limit_2026-10-22.json)).
- **Reason codes only for declines.** Most of the scorer's own time was the TreeSHAP explanation behind its reason codes. The stack now computes them only when the model would decline, which is also where a case opens (`PAYGUARD_EXPLAIN=declines`; `always` restores codes on every score). Scores, decisions and cases are unchanged. At 600/s, three alternating runs per setting cut the scorer's CPU from 2.64 to 2.10 ms per authorization; at 750/s two pairs showed no difference ([record](bench/explain_ab_2026-10-22.json)).
- **Batched Kafka traffic.** The outbox publisher waited for each record's acknowledgement, and both consumers committed their offset after every message. Sending a polled batch before waiting, and committing once per consumed batch, cut Kafka's CPU per authorization from 1.45 to 0.53 ms and the whole stack's from 8.21 to 6.34 ms at 600/s (three alternating runs per build). Delivery is still at least once ([record](bench/kafka_batching_2026-10-22.json)).
- **Rules-only baseline.** The 11 October rules-only run at 500 offered authorizations per second completed 7,501 with 6.077 ms p95 and zero authorization errors, with scoring and event publication outside the measurement.

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

The next checks are lowering CPU per authorization and re-measuring with the load generator on a separate machine, putting an authenticating proxy and the PostgreSQL case store into the packaged stack, and testing settlement against a network-format file from an independent source. No real cards, customer data, or network feed are used here.
