# Bugs found by running it for real (fixed)

Moved from AGENTS.md (2026-11-11), text unchanged.

- Fixture TIFF was big-endian while real files are little-endian, which hid a fetcher byte-order bug.
- `bytes_downloaded` was shared state on the fetcher, so concurrent fetches corrupted each other's counts; now per call (regression test).
- `fetch` computed the Region window time from the already rewritten `scenes.acquired_at`, so every retry shifted it again; the catalogue time now lives in `scenes.acquired_start` (migration 0003) and fetch always derives from it (regression test).
- Manually triggered Airflow runs have no data interval and may have no logical date (only `run_after`); `ais_load` crashed and `scene_pipeline`'s `since` template would have; both now fall back correctly (tests). Scheduled runs were never affected.
- Prometheus scraped `/metrics` but the API serves `/v1/metrics`.
- SeaweedFS health check: the S3 gateway opens later than its master; the check now probes the S3 port.
- Overpass answers 406 to anonymous clients: the loader sends a descriptive User-Agent and retries when the server is busy.
