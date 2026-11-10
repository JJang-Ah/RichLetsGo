# D101 08a synthetic benchmark

**Synthetic data only.** The benchmark created a temporary SQLite database with 600 synthetic DESIGN contracts, 43,000 hourly bars per contract (25,800,000 bars total), and 4-hour funding. Prices were deliberately flat, so the study emitted no trades. No D98 market database or judgment-pool data was opened. The temporary database was removed after both runs.

## Measurements

| Phase | Elapsed | Peak process-tree working set |
|---|---:|---:|
| Build synthetic database | 147.616 s | 36.96 MiB |
| Study, 1 worker | 233.377 s | 89.79 MiB |
| Study, 4 workers | 81.206 s | 401.68 MiB |

The temporary database occupied 2,558,050,304 bytes (2.382 GiB) before cleanup. These elapsed times are wall-clock measurements from this Windows workstation.

## Determinism

- 1-worker body SHA-256: `6763ef33030f9aa73d59ba4f911fcc5f678f4c646649aab19e6e3c41e87ba533`
- 4-worker body SHA-256: `6763ef33030f9aa73d59ba4f911fcc5f678f4c646649aab19e6e3c41e87ba533`
- Body digests match: **yes**
- Trade stream bytes and SHA-256 match: **yes** (both empty; SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`)
- Temporary database removed: **yes**

The machine-readable measurements are in [D101-08a-synthetic-benchmark.json](D101-08a-synthetic-benchmark.json).

These measurements and digests describe the original `f816e9f` implementation. The PR #52 review follow-up adds two registered policy limitations, so a new run's body digest will differ. The original measurements are preserved as historical results; the review follow-up did not rerun this benchmark.
