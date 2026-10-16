# PayGuard measured results

## Kotlin ledger and API

On macOS arm64, Java 17, Gradle 9.7.1, and PostgreSQL 16, the 15 October 2026 offline `./gradlew test` run passed **13 tests**: five domain/property tests and eight PostgreSQL integration tests. The original twelve cover scoring-mode/fallback, settlement classifications, concurrent company limits, and payment lifecycle. The added test checks the journal-backed settlement source. Property checks include 200 FX values and 250 randomized debit/credit pairs. A seeded integration test executed 40 authorization → partial capture → final capture sequences, with refunds on 20, and found no journal with fewer than two postings or a nonzero posting sum. A direct SQL insert of an unbalanced journal failed at commit. These are local tests, not a proof for all schedules, overflow conditions, or failures.

A separate live Ktor REST smoke issued one synthetic card, activated it, approved an SGD 1,250-minor-unit authorization, captured it, and read an available balance of SGD 8,750 minor units from an initial SGD 10,000. `/openapi.yaml` returned HTTP 200.

### k6 authorization throughput

Measured on an **Apple M3 Pro with 18 GiB host memory**, macOS 15.3.1, Java 17, PostgreSQL 16 in Docker. The Kotlin API ran in **rules-only (`PAYGUARD_MODEL_MODE=off`)** mode. Kafka, Redis, Grafana, and Prometheus were paused; the PostgreSQL outbox still wrote events, but its publisher was paused. Each k6 run used a 15-second constant-arrival-rate target and **fresh synthetic cards** with velocity controls raised for load testing. Card setup traffic is excluded from the custom `auth_duration` trend. Raw summaries are in [`bench/`](bench/).

| Offered rate | Fresh cards | Completed auths | Auth p50 | Auth p95 | Auth p99 | Auth errors | Dropped iterations |
|---|---:|---:|---:|---:|---:|---:|---:|
| 100/s | 50 | 1,501 | 5.583 ms | 7.826 ms | 9.344 ms | 0 | 0 |
| 500/s | 100 | 7,501 | 2.994 ms | 6.077 ms | 9.235 ms | 0 | 0 |

The achieved counts match the scheduled iterations over 15 seconds, allowing for one boundary iteration. The 500/s latency is lower because these are separate runs with different card counts and a warmer JVM/database; it is **not evidence that higher load improves latency**. Neither run measures synchronous fraud scoring, Kafka publication, or a production network.

An earlier 100/s run under PostgreSQL `SERIALIZABLE` isolation produced one serialization failure; a second run produced 17. Those raw summaries are preserved as `k6_100_before_fix.json` and `k6_100_serializable.json`. The implementation now uses `READ COMMITTED` with explicit card-row locks, and a company-row lock when company limits apply. A concurrent two-card integration test checks that a shared company limit allows exactly one of two competing authorizations. The successful table above is from the corrected implementation.

## Fraud model and streaming results

All model values below were produced by the checked-in training/evaluation scripts and raw JSON reports in [`bench/`](bench/). Features use only the current request and prior card history; the test split is later in time than training and validation. The primary dataset is **synthetic and intentionally learnable**. Its results do not establish real-world fraud performance.

| Synthetic held-out test (15,560 rows; 830 fraud) | Logistic baseline | LightGBM |
|---|---:|---:|
| PR-AUC | 0.9331 | 0.9574 |
| Brier score | 0.00871 | 0.00525 |
| Recall at validation-selected cost threshold | 93.37% | 92.17% |
| False-positive rate at that threshold | 2.97% | 0.319% |
| Fraud amount-weighted recall | 95.26% | 93.00% |
| Illustrative total cost in simulated minor units | 1,071,841 | 1,087,206 |

The logistic baseline has a slightly lower cost under the **illustrative** cost assumptions even though LightGBM has higher PR-AUC. The service uses the LightGBM threshold of 0.1 as a demonstration choice, not a claim of optimal business cost. Validation-selected thresholds targeting 0.5% or 1% FPR did **not** maintain those rates on the later test split: LightGBM reached 2.315% on either target. The raw report preserves these values rather than hiding the shift.

For the [OpenML 43627 public benchmark](data/SOURCES.md), the later held-out test has 56,962 rows and 75 frauds. Logistic PR-AUC was **0.7614**, LightGBM PR-AUC **0.7733**. The public set has anonymized features and a different feature contract, so its model is separate from the online synthetic model. Its `Amount` units are unspecified; the amount-weighted and illustrative cost fields in the raw JSON are **dataset-unit calculations, not dollars or currency**. The public report also shows threshold drift between validation and test.

The scorer Docker image rebuilds the synthetic model on **Linux ARM64** from the same locked Python dependencies. Its held-out LightGBM PR-AUC was **0.9589** and the validation-selected threshold was **0.09449**, compared with **0.9574** and **0.1** on macOS ARM64. Native LightGBM training and ONNX conversion vary across these platforms; the Linux image's maximum calibrated ONNX difference was **0.00316** over 1,000 checks, within the scripted 0.01 tolerance but larger than the macOS result. The image uses its **own** generated model, threshold, baseline, and export checks. Do not mix macOS model metrics with the Linux image; see [`synthetic_model_linux_results.json`](bench/synthetic_model_linux_results.json) and [`onnx_linux_results.json`](bench/onnx_linux_results.json).

ONNX export checked 1,000 rows: maximum raw probability difference from native LightGBM was **0.000748** and maximum calibrated difference was **0.00000575**. In-process, single-row ONNX-only p95 was **0.007834 ms**. A 200-request sequential localhost HTTP rerun with Prometheus instrumentation, including Redis lookup, feature creation, ONNX, calibration, and reason codes, measured **4.340 ms p50, 6.506 ms p95, 8.863 ms p99**. These are warm local measurements; they do not include concurrent load or the Kotlin auth path. The Kotlin caller sets a 50 ms scoring timeout and falls back to rules on scorer failure; its mode behaviour is covered by a service test. In a live smoke after a scorer restart, the first request took the rules fallback while the model was cold; the next request was declined by the warmed model.

The latest packaged [`demo.py`](fraud/src/payguard_fraud/demo.py) end-to-end run issued a synthetic card, seeded 20 prior normal events, then sent eight tiny authorizations through Kotlin → PostgreSQL outbox → Kafka → Redis. The first request used the bounded **rules fallback** while the model was cold and was approved; the next **seven were declined by the model**, starting at transaction index 1. Kotlin API times were **11.33–108.76 ms** and Kafka-to-Redis observation took **63.91–148.41 ms** after each response. The final event opened a case with a cited `card_testing` Ollama draft pending analyst review. These eight samples are a smoke test, not a throughput benchmark. See [`burst_results.json`](bench/burst_results.json).

## Investigation agent

The LangGraph flow retrieves synthetic case evidence through five read-only tools, requests a local Ollama classification, validates the pattern/action against observed signals, attaches only transaction IDs from retrieved evidence, and returns a recommendation with `PENDING_ANALYST` status. On **40 generated template cases** (30 fraud, 10 benign), the full guarded workflow had **40/40 template pattern matches, 40/40 action matches, and zero invented cited IDs**. The 0.5B local model produced **12 accepted drafts**; **28 used the deterministic rules fallback**. These are workflow safety checks against hand-built labels, **not independent LLM accuracy or analyst agreement**. See [`case_eval_ollama_results.json`](bench/case_eval_ollama_results.json).

A Kafka case consumer ingested the end-to-end burst events into a local SQLite queue and opened cases. The analyst API generated a cited `card_testing` draft for the final burst event using observed timestamps and amounts, with `draftSource=ollama` and `approvalStatus=PENDING_ANALYST`. Analyst approval records a review decision only; it does not execute a card freeze or customer contact.

## Settlement reconciliation

The Kotlin reconciliation service compares a strict three-column synthetic network CSV with the merchant-clearing **journal postings** for captures on a UTC date. Its PostgreSQL integration tests exercise matching, duplicate network rows, amount mismatch, unknown capture IDs, and captures missing from the network file. On 15 October, a regression test inserted one capture record without a journal and another whose balanced journal credited 650 minor units while capture metadata said 700. The service classified a network row for the first as `MISSING_LEDGER`; for the second, it reported `AMOUNT_MISMATCH` with expected amount 650 and observed amount 700. The full 13-test Gradle suite passed, and Testcontainers stopped its PostgreSQL and cleanup containers. This checks that the comparison reads posted money instead of trusting capture metadata; it does not detect a capture missing from both the ledger and network file or prove a real network feed.

An earlier live API smoke generated and reconciled a sample containing one captured SGD 1,250-minor-unit transaction: **1 matched, 0 discrepancies**. See [`settlement_sample.csv`](bench/settlement_sample.csv) and [`settlement_report.json`](bench/settlement_report.json). The sample file is generated from the same local ledger, so it is a functional smoke rather than an independent network feed.

## Observability and console

The React console built with TypeScript/Vite. The packaged Nginx console and its reverse proxy served HTTP 200 for the page, recent authorization events, and cases. The optional Prometheus/Grafana profile started from locally cached digest-pinned images; Prometheus reported both Kotlin and scoring scrape targets **up**, and Grafana's API listed the provisioned PayGuard dashboard. A synthetic pair of authorizations incremented `APPROVED=1`, `DECLINED=1`, `RULES_FALLBACK=1`, and `MODEL_DECLINE=1` counters. The scoring API exposes duration and score histograms plus a PSI gauge after 100 scores. A 200-request benchmark using one synthetic card produced a high PSI; this intentionally narrow traffic mix is not a representative drift assessment.

The Kotlin API, locked Linux scoring, and console Docker images built locally. The frontend's analyst review flow passed its Vitest/React Testing Library check, and the TypeScript/Vite build passed. The GitHub Actions workflow is authored but **has not run remotely**. The local observability start was verified here.
