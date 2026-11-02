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

The first benchmark process stopped during storage when the preceding turn ended (338 committed contracts); that partial synthetic DB is retained locally and is not used for the published measurement. No market data was involved.

## Remaining manual sequence

Claude reviews this implementation, user merges, then run the actual design collection and publish its audit in a separate PR. Pool and D99 are already merged. No judgment data access is permitted; research registration/one-shot study remains 07c. The real audit result cannot be predicted from these synthetic tests.

API references: [Bybit kline](https://bybit-exchange.github.io/docs/v5/market/kline), [funding history](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate).
