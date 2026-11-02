# M2-07b implementation — design collection and coverage audit

This PR implements the D98 design-only collector and timestamp audit from M2-07 §07b. It uses the merged pool artifact and D99 settings. No actual design or judgment price/funding history was downloaded during implementation; fixtures and the benchmark are synthetic.

## Contracts

- `boundaries.py` owns the common range predicate used by collection, coverage and hashing. Hourly bars require `S <= start` and `start + 1h <= min(T,D)`; funding requires `S-24h <= time < T` and `time <= D`. Empty lifetimes are skipped. BTC daily is separate `[2020-11-07,2026-10-09)` input.
- `collect.py` validates the pool digest and requested symbols before requests. `DesignTransport` independently rejects symbols outside DESIGN, unknown hosts/endpoints/parameters, and daily data other than BTC. Requests use only kline and funding/history. `DesignClient` subclasses the existing public client to reuse its RetryPolicy without changing Symbol or the existing client API.
- Klines use bounded 1000-bar pages. Funding uses disjoint 200-hour ranges (at most 200 hourly settlements, including an exact delisting settlement). Each successful page commits rows, checkpoint and attempt history together. Failure rolls back rows/checkpoint and records attempts/error. Resume skips committed pages; explicit `--refresh` revisits the same ranges to repair holes before registration. Conflicting values always fail, including duplicates within a response. Out-of-range responses are retained and counted, but excluded from hash and research inputs.
- `store.py` uses Decimal-normalized text, bounded insert batches, separate hourly/daily/funding tables and pool identity binding. `freeze(registration_digest)` prevents subsequent collection/row writes; 07c registration integration must invoke this when registering the dataset. The collector/auditor use an exclusive sidecar lock. A crashed lock is retained for explicit operator diagnosis, never automatically stolen. Do not edit SQLite directly while collecting or auditing.
- `audit.py` reads only timestamps for coverage decisions: F, L_a/L_e/E_a, allowed/excess delisting tail, internal gaps, metadata eligibility, 800-hour input completeness, three preceding settlements, interval distribution and current metadata mismatch. Funding interval/freshness and price/liquidity tests are stage 4 and belong to 07c, not the input-insufficient numerator. Zero eligible opportunities are reported with ratio null.
- The independent hash pass streams canonical rows in timestamp order and emits the exact `D98-data-v1` manifest. It includes the funding settlement at D even if D=E_a. Parallel audit tasks own separate read-only SQLite connections and results are collected in symbol order. The output body/digest is worker-count independent; runtime metrics are outside the audit.
- Coverage requires all BTC daily bars, and tests insufficient inputs with integer `missing * 20 > eligible` (exact 5% passes). Price mutations change hashes without changing coverage decisions.

## Commands after review/merge

Use a local config with the `[d98.collection]` pacing and worker keys from `config.example.toml`, plus the existing `[paths.backtest] market_data_dir` and `[bybit_public] base_url`. A relative configured data directory resolves against the config file; explicit `--db` overrides it. Collection is serial to obey global request pacing; configured workers bound audit parallelism.

```powershell
python tools/d98_collect.py --pool docs/reports/D98-pool.json --config config.local.toml
python tools/d98_audit.py --pool docs/reports/D98-pool.json --config config.local.toml --output docs/reports/D98-collection-audit.json
```

Fetch origin before collection: the CLI verifies the pool bytes equal the published `origin/main` artifact. Audits use exclusive new output paths, so prior audits remain available. For pre-registration gap recovery, run collect with `--refresh` (optionally `--symbols` restricted to DESIGN) then audit to a new path. No automatic collection on import or test execution.

## Validation

The synthetic tests cover digest and judgment-symbol rejection at both layers; boundaries; exact delisting funding through collection, coverage, manifest and an independent one-unit liquidation R ledger; page resume/retry/failure audit; conflicts; frozen datasets; locking; missing/internal/allowed/excess tail; exact 60 days; metadata precedence; price-invariant decisions; exact 5%/above 5%; BTC missing; canonical hash ordering; outside rows; 1/2-worker equality.

The liquidation ledger is the §07b endpoint regression, not the complete 07c signal simulator. In 07c, connect the same fixture to the production simulation and reuse `window.contains`/`series_hash`; do not implement a second boundary rule.

Benchmark command (synthetic only):

```powershell
python tools/benchmark_d98_collection.py --output backtest_results/d98-07b-synthetic --workers 4
```

It inserts 600 × 43,000 hourly bars plus funding and BTC daily via Store, then compares 1/4-worker audit bodies and data hashes. Memory is OS high-water working set per process; sum of parent and worker peaks is a conservative upper bound, not a simultaneous RSS sample. Timing includes both timestamp audit and streaming hash. Signal-study runtime is measured in 07c, where that code is implemented.

The first benchmark process stopped during storage (338 committed contracts; termination cause not verified); that partial synthetic DB is retained locally and is not used for the published measurement. No market data was involved.

## Remaining manual sequence

Claude reviews this implementation, user merges, then run the actual design collection and publish its audit in a separate PR. Pool and D99 are already merged. No judgment data access is permitted; research registration/one-shot study remains 07c. The real audit result cannot be predicted from these synthetic tests.

API references: [Bybit kline](https://bybit-exchange.github.io/docs/v5/market/kline), [funding history](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate).

## Actual collection request plan (metadata only)

Using the merged 405-contract DESIGN pool and fixed lifetime ranges, a fresh collection plans 7,594 hourly kline pages + 37,159 funding pages + 3 BTC daily pages = **44,756 requests**, before retries. At the example 0.2-second pause this alone takes **2.4864 hours**, plus network latency and retry backoff. Funding intentionally uses fixed 200-hour pages to accommodate hourly settlement and make page recovery deterministic; sparse/8-hour histories incur more requests than backwards record-count pagination. These counts were calculated from metadata only; no availability probe or price request was made.

Final local validation: Python 3.14.3 **894 passed in 70.15 seconds**, including 22 new collection/audit cases and 77 pool cases. Ruff check/format and strict mypy (89 source files) pass.

## Final benchmark (code 879173fac5c51f71a484c7f7e041f7369b1d62f1)

Published raw measurements: `D98-07b-synthetic-benchmark.json`.

| Operation | Seconds | Peak working-set measure (MiB) |
|---|---:|---:|
| Store 600 x 43,000 bars + funding/daily | 140.419 | 38.598 |
| Audit + hash, 1 worker | 205.691 | 49.160 |
| Audit + hash, 4 workers | 78.180 | 225.652 (sum of process peaks) |

Both full audit bodies/digests and data hashes are identical. Data hash: `de560629d5e97b3c4ce70ff28d0ee2d01c93af1bdb4a371628bee258ef5a0420`. Audit digest: `79ef17e94bd10d5e5d181ed67bb7ccb8d5208548b338814e6158bd1778c3581d`. The benchmark contains synthetic data and no network attempt logs; actual collection-log metadata adds report memory. This is data preparation performance, not trading performance. A preceding development measurement is retained locally; only the final committed-code run above is published.

## PR #43 review follow-up (Claude 6571548433)

The audit now carries a streaming `collection_log` summary instead of all normal page records: executed pages (including failed/refresh executions), requests, retried pages/total retries, error pages/error messages, refresh pages, skipped empty-lifetime symbols, collection-start records and a full-log SHA-256. Retry/error/refresh rows remain unmodified in `exceptional_rows`; the DB retains every original row.

Hash contract: concatenate each stored `collection_log.content` in ascending id order, append one LF to each, encode UTF-8, and SHA-256 those **uncompressed** bytes. `d98_audit.py --export-log <path.jsonl.gz>` streams exactly those bytes into an exclusive new gzip file under the same dataset lock as the audit. Compare the decompressed file SHA-256 with `body.collection_log.sha256`. Normal pages are counted without accumulating them in memory; exceptional records necessarily grow when many requests fail or are refreshed.

Each CLI collection invocation logs its executing git commit, start timestamp, selected symbols and refresh flag before requesting pages. Audit code provenance remains separate. Library fixtures may omit the optional commit argument.

`launch_partial_hour` identifies a non-hour-aligned launch's preceding partial bar and whether it is absent. The mandated floor-hour expected range and raw `missing_ranges` are unchanged, and F/eligibility still use actual launch. This is explanatory metadata, not permission to omit otherwise required data. The price-invariance fixture now mutates open/high/low/close/volume/turnover and funding rate together.

The published large benchmark remains the historical measurement of commit 879173f. This review changes audit presentation/digest, not the D98-data-v1 manifest/data_hash or series processing. A focused 44,756-normal-page regression confirms summary size stays below 1KB, and the synthetic worker comparison confirms the revised audit body remains deterministic. No large benchmark rerun or real market-data request was made for the log-format change.
