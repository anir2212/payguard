# PayGuard measured results

## Batched Kafka publishing and offset commits, 22 October 2026 (evening)

Kafka was the third-largest CPU user per authorization (about 1.4 ms). The outbox publisher waited for each record's acknowledgement and ran one UPDATE per row, and both Python consumers made a synchronous offset commit after every message. That is three broker round trips per authorization before any fetches. Now the publisher sends a whole polled batch (up to 100 rows), flushes once, checks every acknowledgement and marks the rows with one `UPDATE … WHERE id = ANY(?)`. The consumers take up to 500 messages per `consume()` call and commit once after the whole batch is applied. Record: [`bench/kafka_batching_2026-10-22.json`](bench/kafka_batching_2026-10-22.json).

Same method as the section below, at 600/s with `PAYGUARD_EXPLAIN=declines` in both builds. The old images were built from the previous version of the code, and builds alternated (old, batched, batched, old, old, batched). Medians of three runs, with ranges:

| CPU per authorization | Before | Batched |
|---|---:|---:|
| Kafka broker | 1.45 ms (1.38–1.49) | 0.53 ms (0.39–0.59) |
| Outbox publisher | 0.67 ms (0.61–0.69) | 0.35 ms (0.27–0.40) |
| Feature consumer | 0.53 ms (0.51–0.53) | 0.19 ms (0.17–0.21) |
| Case consumer | 0.42 ms (0.36–0.43) | 0.34 ms (0.32–0.34) |
| Whole stack | 8.21 ms (7.71–8.47) | 6.34 ms (5.34–7.17) |

- **Kafka's CPU fell by about 60%,** and every batched run was below every old run. The stack total fell by about 23%; the highest batched run is still below the lowest old one.
- **The case consumer barely changed,** because each event still costs a write to its SQLite store.
- **Other services did not move consistently.** The API, PostgreSQL and the scorer vary more between runs (the scorer 1.85–2.42 ms) than any effect here.
- **Delivery is still at least once.** Rows are marked published only after every record in the batch is acknowledged, and a failed send rolls the batch back for a later retry. Consumers commit only after the whole batch is applied, so a crash replays at most one batch, and both stores already ignore repeated authorization IDs. New tests cover each of these: two Testcontainers tests for the publisher and four for the consumers.
- All batched runs completed 9,000 or 9,001 of 9,001 authorizations with no errors.

Limits: one machine with k6 on it, three runs per build at a single rate (the knee was not re-measured), and a synthetic workload. A shared lock kept the other heavy jobs on the laptop paused during these runs, but light work may still have run.

## Reason codes only for declines, 22 October 2026 (evening)

In process, `Scorer.score` took 579 µs per call, and 514 µs of that was LightGBM's TreeSHAP for the reason codes (3,000 calls, one thread). The trees are deep (mean depth 13.5, maximum 22), and TreeSHAP's cost grows with the square of depth. `PAYGUARD_EXPLAIN=declines` skips it unless the score reaches the 0.1 decline threshold, which is also where a case opens. A test checks that scores, decisions and every declined result's codes are identical under both settings. Record: [`bench/explain_ab_2026-10-22.json`](bench/explain_ab_2026-10-22.json).

Each run used a fresh database, the default 4 scorer workers × 1 thread, a 100/s warm-up and a measured 15 seconds of `bench/load_full.js`, with the two settings alternated. CPU here is each container's cumulative cgroup time over the measured window, divided by completed authorizations. It leaves out k6 and Docker's VM, so it reads lower than the docker-stats estimate in the next section; compare it only within this table.

| Offered | Setting | Runs | Stack CPU per authorization | Scorer CPU per authorization | p99 | Dropped |
|---:|---|---:|---:|---:|---:|---:|
| 600/s | always | 3 | 8.34 ms (8.17–8.54) | 2.64 ms (2.52–2.66) | 461 ms (368–645) | 31 (14–118) |
| 600/s | declines | 3 | 7.69 ms (7.55–7.89) | 2.10 ms (2.09–2.15) | 348 ms (281–364) | 0 (0–15) |
| 750/s | always | 2 | 8.10, 8.63 ms | 2.67, 2.90 ms | 375, 625 ms | 0, 61 |
| 750/s | declines | 2 | 8.56, 8.87 ms | 2.62, 2.63 ms | 414, 569 ms | 12, 83 |

Medians with ranges at 600/s; both values at 750/s.

- **At 600/s it helps consistently.** All three `declines` runs used less scorer and stack CPU than all three `always` runs: about 0.5 ms less scorer CPU per authorization (20%) and 8% less across the stack.
- **The saving is limited by this workload.** 57% of its authorizations score at or above 0.1, so TreeSHAP still runs for most of them; only the model's approvals are spared.
- **At 750/s there was no measurable difference** in two pairs. Near saturation, run-to-run contention is larger than the saving.
- **The knee is approximate.** One `always` run at 750/s completed every scheduled authorization, which the single 750/s run in the next section did not.
- **What changes for callers.** Authorizations the model approves now return an empty `modelReasonCodes` list. Model declines and cases keep their codes. `PAYGUARD_EXPLAIN=always` restores codes on every score.

Limits: one machine with k6 on it, three runs per setting at 600/s and two at 750/s, and a synthetic workload and model.

## Where the scored path saturates, 22 October 2026 (late afternoon)

Same method as the repeated runs below: each run started from a fresh database (its own Compose project and new volumes), with a 100/s warm-up before the measured 15 seconds. The setting was the default 4 scorer workers × 1 thread, with the keep-alive fix. During each run, the API's PostgreSQL connections and the containers' CPU were sampled; the samplers add a little load of their own. These are single runs. Record: [`bench/full_path_limit_2026-10-22.json`](bench/full_path_limit_2026-10-22.json).

| Offered | Completed / dropped | p50 / p95 / p99 | Rules fallback | API pool: idle in transaction / idle | Container CPU (of 1,100%) |
|---:|---:|---:|---:|---:|---:|
| 600/s | 9,001 / 0 | 20.7 / 208 / 394 ms | 225 | 12.8 / 4.5 | 732% |
| 700/s | 10,501 / 0 | 35.6 / 235 / 409 ms | 263 | 14.4 / 2.6 | 740% |
| 750/s | 10,932 / 318 | 459 / 770 / 973 ms | 352 | 16.2 / 0.3 | 783% |
| 850/s | 9,273 / 3,477 | 1,259 / 1,656 / 1,878 ms | 702 | 16.3 / 0.8 | 800% |

- **The knee is about 730 authorizations a second.** 600 and 700/s completed every scheduled authorization with no errors, but the tail was heavy (p99 about 400 ms). At 750/s all 20 pooled connections were in use, latency jumped and k6 dropped iterations. At 850/s fewer authorizations completed than at 750.
- **More scorer workers barely help.** 6 or 8 workers at 850/s raised throughput by about 10%. Scorer CPU stayed near 285% either way.
- **The laptop runs out of CPU.** At 800/s macOS reported 1–2% idle: Docker's VM used 550–720% and k6 50–145%. All the services and the load generator share the same 11 cores, so the ceiling is total CPU rather than one component. The connections idle in transaction are waiting on a scorer that slows down as the machine fills.
- **Where the CPU goes.** At 750/s the stack used about 11 ms of CPU per authorization: scorer 3.7 ms, API 2.4, PostgreSQL 1.5, Kafka 1.4, outbox 0.7.
- **Inside the scorer.** In process, a full score costs 0.61 ms (2,000 calls, one thread). LightGBM's TreeSHAP for the three reason codes is 0.56 ms of that; the ONNX model run is 0.007 ms.
- **Contention, not serving code.** Loaded alone at 500/s, with only Redis running beside it, the scorer container used **1.2 ms** of CPU per request. Serving (HTTP, FastAPI and pydantic, the Redis lookup) therefore adds about 0.6 ms. The 3.7 ms per authorization in the saturated stack is mostly the cost of CPU contention.
- **httptools and uvloop.** Swapping uvicorn's pure-Python h11 server for the C parser and uvloop cut the scorer-alone cost by 18% in one A/B pair and 6% in the other (1.23 and 1.24 ms against 1.01 and 1.17 ms). That is too small and inconsistent to adopt yet.

Next steps: run the load generator on a separate machine, and lower CPU per authorization across the services. For example, compute TreeSHAP reason codes only where they are shown, which is about half of the scorer's own cost (done that evening; see the section above). Limits: one machine, sampling overhead, single runs, and a synthetic workload and model.

## Repeated load runs, the 500/s ceiling and its fix, 22 October 2026 (afternoon)

Every run here started from a fresh, empty database: its own Compose project with new volumes, removed afterwards. The project's usual volumes were not touched. Each run had a 15-second warm-up at 100/s, a 10-second pause, then the measured 15 seconds of `bench/load_full.js`. There were three repeats of every setting and rate, with the order of settings rotated between repeats. Other applications on the Mac were light this time. Record: [`bench/full_path_repeat_2026-10-22.json`](bench/full_path_repeat_2026-10-22.json); raw k6 summaries: [`bench/full_path_repeat_k6/`](bench/full_path_repeat_k6/).

**Before the fix** (images from commit `c11b991`). Medians of three runs, with the range of rules fallbacks:

| Setting | 250/s p50 / p95 / p99 | 250/s fallback | 500/s completed / dropped | 500/s p50 | 500/s fallback |
|---|---:|---:|---:|---:|---:|
| 1 worker, default threads, V3/V4 indexes dropped | 25.0 / 85.9 / 145.0 ms | 622 (222–996) | 5,426 / 2,075 | 1,346 ms | 4,840 |
| 1 worker, default threads | 23.8 / 82.0 / 138.4 ms | 379 (184–2,245) | 5,572 / 1,928 | 1,303 ms | 4,560 |
| 4 workers, 1 thread each | 4.7 / 49.6 / 153.5 ms | 4 (0–20) | 6,311 / 1,190 | 1,131 ms | 7 (4–71) |

The repeat confirmed that 4 workers × 1 thread almost removes rules fallback at 250/s. On a fresh database the indexes made no clear difference, as expected: a 15-second run does not grow the tables enough. Their benefit is the growing-table measurement in the follow-up section below.

**Why 500/s still saturated.** At 500/s, 4, 6 and 8 single-thread workers all completed about 430 authorizations a second, although the scorer rarely missed its 50 ms deadline. The diagnosis, step by step:
- Sampling `pg_stat_activity` during a 4 × 1 run showed all 20 of the API's pooled database connections in use, about 19 of them "idle in transaction". The API holds a connection, and the card row lock, while it waits for the scorer. With the model off, the same load left the pool nearly idle and ran at 1.4 ms p50.
- k6 sending the same requests straight to the scorer at 500/s got 2 ms p50 with 4 workers, so the scorer was not slow.
- A Java `HttpClient` probe with the API's settings took 44–49 ms per call to the scorer even with one thread on an idle stack. So did a Python client inside the scorer container, on `GET /health` as well as `POST /score`. A new connection per request took 1.4 ms; only keep-alive connections stalled. With one uvicorn worker a keep-alive call took 1.3 ms, with two it took 48 ms.
- The cause is in uvicorn 0.54. With `--workers` above 1, uvicorn creates the listening socket itself, with protocol number 0. asyncio turns on `TCP_NODELAY` only for sockets whose protocol is TCP, so every worker connection kept Nagle's algorithm on. uvicorn writes the response headers and body separately, and on a keep-alive connection the body waited for the client's delayed acknowledgement, about 40 ms on Linux. Twenty connections at about 46 ms each allow roughly 435 authorizations a second, which matches what was measured. k6 on the Mac never saw this because Docker Desktop's port forwarder acknowledges immediately.

**The fix.** [`nodelay.py`](fraud/src/payguard_fraud/nodelay.py) is a small subclass of uvicorn's h11 protocol that sets `TCP_NODELAY` on every connection, and Compose starts the scorer with `--http payguard_fraud.nodelay:NoDelayH11Protocol`. With 4 workers a keep-alive call now takes 1.3 ms. The Java probe went from 48.9 ms to 1.5 ms per call with one thread, and from 444 to 1,698 calls a second with 20 threads. Three new tests cover the socket option and uvicorn loading the class.

**After the fix.** Medians of three runs:

| Setting | 250/s p50 / p95 / p99 | 250/s fallback | 500/s completed / dropped | 500/s p50 / p95 / p99 | 500/s fallback |
|---|---:|---:|---:|---:|---:|
| 1 worker, default threads | 31.8 / 99.2 / 185.0 ms | 864 (815–1,851) | 5,319 / 2,181 | 1,393 / 1,847 / 2,111 ms | 4,736 |
| 4 workers, 1 thread each | **4.3 / 17.5 / 74.4 ms** | **11 (3–20)** | **7,501 / 0** | **6.9 / 75.1 / 173.7 ms** | **43 (22–132)** |

With the fix, 4 workers × 1 thread completed every scheduled authorization at 500/s in all three runs, with 0.3–1.8% rules fallback and no errors. One worker is unaffected by the fix (single-worker uvicorn already set `TCP_NODELAY`) and still saturates at 500/s, because one process is CPU-bound. **Compose now defaults to 4 workers with 1 thread each**, and a plain start was checked to run 4 worker processes with the new protocol.

**The next limit.** Single exploratory runs with 4 × 1 saturated at 750/s (10,233 completed, about 680 a second, with 1,017 dropped and 5% fallback) and at 1,000/s (11,314 completed, 3,686 dropped). Fallback stayed low, so the scorer deadline is not the main limit there. The candidates are the API's 20-connection pool held during the scorer call, scorer CPU and PostgreSQL commit cost. This has not been diagnosed.

**Correction.** The follow-up section below said 500/s still saturated with 4 × 1, "so the scorer is no longer the only limit". The limit was this keep-alive stall in the multi-worker scorer, made worse because the API holds a pooled connection while it waits. The first batch's 1-worker runs did not have the stall; there, the single process was CPU-bound.

Limits: a synthetic workload and model on one machine over Docker networking, not a production capacity claim. There are three repeats per cell, with no confidence intervals. The 750/s and 1,000/s figures are single runs. Tests: pytest 25 passed and 2 skipped (3 new), Ruff clean. The Kotlin code did not change, so Gradle was not rerun.

## Load-test follow-up: scorer settings and two database fixes, 22 October 2026

These runs came after the first batch below, while the database was growing and other applications were loading the Mac (load average up to 24.7). Treat the latencies as indicative. Record: [`bench/full_path_followup_2026-10-22.json`](bench/full_path_followup_2026-10-22.json).

- **Scorer settings.** The scorer can now run several worker processes (`PAYGUARD_SCORE_WORKERS`, default 1) and cap ONNX Runtime and LightGBM threads per request (`PAYGUARD_SCORER_THREADS`, default 0, meaning no cap). A test shows the cap changes no score, decision or reason code. Results:

  | Setting | 250/s p50 / p99 | 250/s fallback | 500/s p50 | 500/s fallback | 500/s dropped |
  |---|---:|---:|---:|---:|---:|
  | 1 worker, default threads (first batch) | 16.2 / 69.0 ms | 114 | 1,254 ms | 4,005 | 1,703 |
  | 4 workers, default threads | 51.4 / 216.5 ms | 50 | 1,424 ms | 167 | 2,283 |
  | 4 workers, 1 thread each | **3.8 / 59.3 ms** | **10** | 1,058 ms | 12 | 976 |

  With default threads, each request used one thread per core: the scorer container peaked at 807% CPU while the API stayed under 45%. Extra workers therefore oversubscribed the CPU and made latency worse. Capping each worker to one thread gave the best 250/s figures measured. 500/s still saturated, so the scorer is no longer the only limit. The defaults are unchanged; the best setting here was 4 workers with 1 thread each. *(Later the same day the repeated runs above traced that 500/s limit to a keep-alive stall in the multi-worker scorer, fixed it, and made 4 workers × 1 thread the default.)*
- **Outbox polling scanned the whole table.** The publisher's pending-rows query had no matching index. At 83,041 rows each poll took 48 ms, every 100 ms while idle, which is why PostgreSQL used CPU with no traffic and why later runs got slower. Migration `V3__outbox_pending_index.sql` adds a partial index on pending rows; the same poll now takes 0.04 ms.
- **The journal-balance check scanned all postings.** The deferred trigger that verifies each journal at commit sums postings by `journal_id`, which had no index. On about 184,000 postings each check took 7–30 ms, and 200 card issues took 10.5 s. Migration `V4__postings_journal_index.sql` adds the index: each check now takes 0.13–0.40 ms, and 200 card issues took 4.2 s (busy host, indicative).
- **Tried and not adopted.** Calling the scorer before opening the authorization transaction would stop the scorer wait from holding a pooled connection and the card-row lock. A test confirmed that the current code does hold the card lock while scoring. But an interleaved A/B at 250/s could not separate the two variants, because both degraded with database growth and host load. Without a clean result the payment path was left as it was.
- **Tests:** Gradle 21 (2 new index-plan tests), pytest 22 passed and 2 skipped (1 new thread-cap test), Ruff clean.

## Full scored path under load, 22 October 2026

The whole packaged stack ran (`make up`): PostgreSQL 16, Redis 7.4, Kafka 3.9.1, the Kotlin API with `PAYGUARD_MODEL_MODE=enforce`, the outbox publisher, the Python ONNX scorer (one uvicorn process), the feature and case consumers, the case API and the console. k6 2.3.0 ran natively on the same Apple M3 Pro (18 GiB; Docker Desktop with 11.67 GiB) using the new mixed workload `bench/load_full.js`:
- one fresh card per authorization, each on its own device and usual merchant category;
- amounts from SGD 1.20 to 800, and merchant risk from 0.05 to 0.9;
- about 3% abroad, plus 4% small-amount bursts on one card from a new device.

Each run lasted 15 seconds, with card setup excluded from timing. "Cold" means the API and scorer containers were restarted just before the run. The rules-only rows used the same workload and the same running stream, with only the API's model mode switched off. Summary: [`bench/full_path_load_2026-10-22.json`](bench/full_path_load_2026-10-22.json); raw k6 summaries: [`bench/full_path_k6/`](bench/full_path_k6/).

| Mode | Offered | Completed | Dropped | p50 | p95 | p99 | Rules fallback | Queues drained after run | Cases opened |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Scored, cold | 100/s | 1,501 | 0 | 10.0 ms | 20.3 ms | 33.2 ms | 0 | ≈2 s | 823 |
| Scored, warm | 100/s | 1,500 | 0 | 8.7 ms | 20.3 ms | 42.0 ms | 2 (0.1%) | ≈3 s | 821 |
| Scored, warm | 250/s | 3,751 | 0 | 16.2 ms | 52.5 ms | 69.0 ms | 114 (3.0%) | ≈6 s | 1,986 |
| Scored, warm | 500/s | 5,798 | **1,703** | **1,254 ms** | 1,653 ms | 1,944 ms | **4,005 (69%)** | ≈9 s | 979 |
| Rules only | 100/s | 1,501 | 0 | 1.8 ms | 3.0 ms | 4.3 ms | – | ≈2 s | 0 |
| Rules only | 250/s | 3,750 | 0 | 1.6 ms | 2.6 ms | 4.3 ms | – | ≈3 s | 0 |
| Rules only | 500/s | 7,501 | 0 | 1.5 ms | 3.4 ms | 15.7 ms | – | ≈3 s | 0 |

No run had an HTTP or authorization error. The scored path handled 250/s with a p99 of 69 ms and 3% fallback. At 500/s it saturated: requests queued behind the single scorer process, 69% of completed authorizations fell back to rules after the 50 ms scorer limit, latency rose above a second, and k6 dropped 1,703 of 7,501 scheduled iterations because no virtual user was free. Rules-only on the same workload handled 500/s at 3.4 ms p95, so the scorer, not PostgreSQL or Kafka, set the ceiling. In every run the outbox, feature consumer and case consumer caught up within about 10 s of the run ending (polled every few seconds, so these times are approximate). Cases open only from a model score, so rules-only runs opened none.

The decline share (58% at 100/s) comes from this synthetic workload and model, not from any fraud rate. Two earlier workload designs gave cards several purchases seconds apart, which the synthetic model treats as near-certain fraud (85–98% declined). They were discarded before these measurements. These are single runs on one machine. They are not comparable with the 11 October rules-only runs below, which used a different script, an older k6 and a paused stream.

## Analyst identity, review history and shared case store, 20 October 2026 (evening)

All data here is synthetic seeded events. Docker was not used, and the Kotlin API was unchanged, so the 19 Gradle tests were not rerun.

- **Python tests.** `pytest` passed **21 tests with 2 skipped**, up from 9, and Ruff passed. Seven governance tests cover the role rules (an `analyst` may view and draft but cannot review, read or purge the audit trail), a lead's decision being recorded once with history and audit rows, no re-drafting after review, audited reads, re-ingestion not reopening a case, the retention floor, and an old SQLite file gaining the new columns. Five API tests show that local mode ignores a name sent in the request body, that proxy mode answers 401 for a missing or wrong token (or no configured token) and 403 for an unknown role or missing user, that an analyst gets 403 on review and audit, and that a second review gets 400 and an unknown case 404.
- **PostgreSQL (opt-in, 2 tests).** A disposable PostgreSQL 18.4 cluster was started from the Homebrew binaries, with its data in a temporary folder and listening only on 127.0.0.1:55433. Both tests passed: the store survives reopening with its history and audit trail, and when two store instances review the same case concurrently, exactly one review is recorded. The race test passed 5 of 5 repeated runs. The cluster was then stopped and deleted, and the everyday PostgreSQL on port 5432 was not touched. `psycopg` 3.3.6 is the new optional `postgres` extra.
- **Frontend.** The console no longer has an analyst-name field. It shows "Recording as *name* (*role*)" from `GET /me`, disables the review buttons for `analyst`, and lists the review history. Vitest passed **3 tests** (one new) and `npm run build` passed.
- **Headless-Chrome case review: 10/10** ([evidence](bench/case_review_browser.json)). This used Chrome 155.0.8059.85 and Playwright 1.64.0, the new optional `browser` extra. The case API ran in proxy mode behind the Vite dev proxy, with browser-set headers standing in for an authenticating proxy, a disposable SQLite store, and Ollama off, so the rules fallback wrote the draft. Without headers the console showed the 401 message and no cases. The analyst drafted a case whose four citations (`auth_small_0` to `auth_small_3`) all appear in its evidence list, and saw disabled review buttons and no name field. The lead's approval appeared as "APPROVED by lee (lead)" with the note, after which the buttons disappeared. The audit trail named `ana` for `draft_saved` and `lee` for `review_recorded`, a second review was refused, and the analyst got 403 on `/audit`.
- **Not done.** No real identity provider or proxy. The packaged Compose stack still uses SQLite and local mode, and was not run. There was no full scored-path load test at that point (it was run on 22 October; see above).

## Settlement integrity audit and synthetic settlement fixture, 20 October 2026

`./gradlew test` (Java 21.0.12.1, Gradle 9.7.1, PostgreSQL 16 through Testcontainers using the cached `postgres:16` image) passed **19 tests, 0 skipped**, up from 13. The six new tests cover the capture-to-journal audit:
- a clean capture is reported as posted;
- a capture missing from **both** its journal and the network file is reported as `MISSING_JOURNAL`;
- duplicate capture journals are flagged;
- a wrong-currency journal is flagged and never counted as settled;
- an amount mismatch is caught even when the network row agrees with the posting;
- the full synthetic fixture runs through the reconciler without the harness reading labels.

The audit is available as `GET /v1/settlements/integrity?date=…` and in every reconcile report. Python `pytest` passed **9 tests**, two of them new fixture tests. The frontend passed 2 Vitest tests, one of them a new journal-audit panel check, and the build passed.

The fixture `payguard-settlement-synthetic-v1` (seed 20261020, settlement date 2026-01-15) is generated by `fraud/src/payguard_fraud/settlement_fixture.py` from its own case table, not from PayGuard's sample endpoint. It has 51 ledger captures with injected journal faults, 51 network rows and 105 labels kept in a separate file the reconciler never reads. A regeneration on 20 October produced byte-identical CSV files. Scoring the harness output gave **105/105 labels correct**, with 0 unexpected predictions:
- 46 discrepancy positives, all flagged with the exact class;
- 59 clean negatives, with 0 false discrepancies;
- every class correct, including 6 `MISSING_JOURNAL` (3 of them also absent from the network file), 3 duplicate-journal, 3 wrong-currency and 6 journal-amount-mismatch cases.

The output and score are in [`bench/settlement_fixture/reconciler_output.json`](bench/settlement_fixture/reconciler_output.json) and [`bench/settlement_fixture/score.json`](bench/settlement_fixture/score.json). **Limits:** the same author wrote the case specification and the reconciler, and the labels follow by construction. The score shows that the code implements the documented classification on inputs it did not generate. It is not real card-network evidence, and the fixture is not independent real data.

**Remote CI (public snapshot only).** GitHub Actions run [39288317832](https://github.com/anir2212/payguard/actions/runs/39288317832) for public PR #2 (commit `3fcdeb0c`) passed its `kotlin`, `python`, `frontend` and `images` jobs on 19 October 2026 (UTC). That run is of the public snapshot repository, before the settlement audit was added; it does not cover these changes.

## Kotlin ledger and API

On macOS arm64, Java 17, Gradle 9.7.1, and PostgreSQL 16, the 15 October 2026 offline `./gradlew test` run passed **13 tests**: five domain/property tests and eight PostgreSQL integration tests. The original twelve cover scoring-mode/fallback, settlement classifications, concurrent company limits, and payment lifecycle. The added test checks the journal-backed settlement source. Property checks include 200 FX values and 250 randomized debit/credit pairs. A seeded integration test executed 40 authorization → partial capture → final capture sequences, with refunds on 20, and found no journal with fewer than two postings or a nonzero posting sum. A direct SQL insert of an unbalanced journal failed at commit. These are local tests, not a proof for all schedules, overflow conditions, or failures.

A separate live Ktor REST smoke issued one synthetic card, activated it, approved an SGD 1,250-minor-unit authorization, captured it, and read an available balance of SGD 8,750 minor units from an initial SGD 10,000. `/openapi.yaml` returned HTTP 200.

### k6 authorization throughput (rules-only, 11 October)

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

The Kotlin API, locked Linux scoring, and console Docker images built locally. The frontend's analyst review flow passed its Vitest/React Testing Library check, and the TypeScript/Vite build passed. At that time the GitHub Actions workflow was authored but **had not run remotely**; see the 20 October note above for the later public-snapshot run. The local observability start was verified here.
