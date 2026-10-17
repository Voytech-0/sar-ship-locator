# AGENTS.md

Guidance and current state for agents working in this repo. Vocabulary is in `GLOSSARY.md`, decisions in `docs/adr/`, the build plan in `docs/PLAN.md`, interfaces in `contracts/`.

## Project

Automated pipeline: Sentinel-1 GRD scenes of the Great Belt are ingested daily, ships are detected (CA-CFAR), Detections are validated against Danish AIS (about 3 days late) and stored in PostGIS. The pipeline and its structure are the portfolio showpiece, so tests per component and contract-first work matter more than detection accuracy.

## Rules

- No `Co-Authored-By` or other attribution lines in commits or PR descriptions.
- Never print or commit secrets. Credentials live in the uncommitted `.env` (S3 keys only are needed; the CDSE username and password are not).
- Build against `contracts/`. Do not change a contract to suit one component: record the issue below and get a decision.
- `make ci` runs lint, mypy, tests and the schema check. Tests that need Docker skip when it is missing.
- Not yet verified on GitHub: the CI workflow has never run.

## Layout

`contracts/` API, schema, protocols, config, metrics, storage. `src/pipeline/` package. `dags/` Airflow (own uv project). `deploy/` Dockerfiles and compose pieces. `migrations/` Alembic. `tests/` including `tests/fixtures/`. `docs/` plan and ADRs.

## Status (2026-10-17)

Steps 0 and 1 (contracts, scaffolding, the seven streams) are merged into `main`. Step 2 (real stages) is done: all five stage handlers run end to end on the committed fixtures. CI has run on GitHub and passed.

Gate: ruff and strict mypy clean; `uv run pytest` about 306 passed, 4 live tests skipped (Docker needed); `dags/` has its own project: `cd dags && uv sync && uv run pytest` (39 passed).

## How the pieces fit

- `src/pipeline/stages/real.py`: `make_registry(Services)` returns the five handlers. `stages/build.py` builds production `Services` from env (S3 keys, MinIO, `PIPELINE_LAND_POLYGONS`); `api/main.py` is the production entry point (`uvicorn --factory pipeline.api.main:app`). Tests inject fakes via `Services`.
- Between `fetch` and `detect` the raster lives in the object store (`contracts/storage.md`: `sigma0.npy`, `geo.npz` lattice, `meta.json`); `detect` rebuilds `SceneRaster.geolocate` from the lattice, masks land with `LandMask.not_land_mask()`, then flags near-shore with `classify()`. Chips (png + npy) are written per Detection; the raster is deleted after detect when retention says so.
- `fetch` sets `scenes.acquired_at` to the time of the Region window's centre row (catalogue start + row * azimuth interval from the annotation), because AIS matching needs the time the ships were imaged, not the scene start. Per-detection row time is not used yet (about +-5 s across the window, under 50 m for a 15 kn ship).
- `/metrics` serves `pipeline.metrics`; DB-mirroring gauges are refreshed at scrape time; handlers and jobs record counters. `/healthz` also checks the object store in production.

## Results worth knowing

- **Partial download (ADR-0004):** only the VV rows covering the Region are fetched: 399 MB in 9 requests, 18 s for the whole Great Belt window (full VV file 883 MB). The TIFF directory (IFD) is at the END of the file, and real Sentinel-1 GRD TIFFs are LITTLE-endian (`II`). The first fixture was big-endian by mistake and exposed a fetcher bug (raw bytes read in the wrong order); both are fixed and covered by a test.
- **End to end on real data:** the fixture scene (a real crop) through discover, fetch, detect, ais-load, validate matches the three known ships to their real AIS vessels (120, 118, 227 m), with the island false alarm removed by the land mask. The test uses only a tiny hand-made land polygon, so the rest of the coast produces many extra unmatched Detections (98 total); real behaviour needs the OSM land polygons.
- **Live DMA AIS:** latest day 3 days old; one day is 293,752 positions for the Region, loaded in about 59 s (now batched with `executemany`).
- **Geolocation:** one affine over the full window is off by up to 197 m; the pipeline uses the GCP-based `geolocate` everywhere positions matter.
- **API:** a succeeded Job dedupes forever on (stage, input) (the `discover` DAG sends a changing `since`). Restarts fail interrupted Jobs as retryable (single API instance assumed). `detect` is allowed only on `fetched` Scenes.
- **Airflow:** 3.3.2, deferrable operators (needs a triggerer). `ais_load` requests day D-3 and two earlier days, and skips (stays green) when a day is unpublished.
- **Observability:** Grafana dashboards load and SQL panels work against a live stack; rate/increase panels returned no data in the short test and are unproven.

## Open items

1. **Sea-level sigma0 looks high:** open water median about -12.5 dB, expected about -20 to -25 dB. Formula `DN^2 / sigma0_LUT^2` looks right; possibly a windy day. Cross-check against SNAP or another calibrated source before tuning CFAR. Thermal noise is not subtracted.
2. **Land polygons (done, data not committed):** `make land` (module `pipeline.landpolygons`, extra `land`) downloads OSM land polygons once (0.9 GB, cached in `data/cache/`), clips them to the Region plus 0.1 degrees, dissolves tile seams and writes `data/land/great-belt.geojson` (ODbL, attribution kept in the file). On the fixture scene real land cuts Detections from 98 to 19: exactly the three known ships are open water (4-12 m from their expected positions) and 16 are flagged near-shore; nothing falls on land. Regression test: `tests/landpolygons/test_real_land_on_fixture.py` (skipped without the file). Still to do: check CFAR false alarms in open water on more scenes, and build the file inside the deployment (compose init job).
3. **Compose stack:** `pipeline-api` (`PIPELINE_DATABASE_URL`, `PIPELINE_LAND_POLYGONS`, S3 and MinIO env, port 8000, after migration), PostGIS (`deploy/postgis`), MinIO with bucket `sar`, Prometheus, Grafana (mounts and pinned images in `deploy/prometheus`, `deploy/grafana`), Airflow (merge `deploy/airflow/compose.yaml`, same network as `pipeline-api`). Grafana needs a login role: `CREATE ROLE grafana LOGIN PASSWORD '...' IN ROLE grafana_ro`, `GRAFANA_DB_PASSWORD`, database named `sar`. The migration job needs both `migrations/` and `contracts/`.
4. **`.env.example`:** add `AIRFLOW_DB_PASSWORD`, `AIRFLOW_FERNET_KEY`, `AIRFLOW_JWT_SECRET`, `AIRFLOW_API_SECRET_KEY`, `GRAFANA_DB_PASSWORD`, `MINIO_ENDPOINT_URL`, `PIPELINE_LAND_POLYGONS`.
5. **CI:** add a `dags/` job (`uv sync && uv run pytest` inside it); `scripts/check_schema.sh` still uses the amd64-only `postgis/postgis:16-3.4`; switch to `deploy/postgis` or replace by `tests/db`.
6. **ADR-0003 vs API:** a second Detector version cannot run on a `detected` Scene via the API; deferred until the ML layer exists.
7. **Docs:** amend ADR-0004 (IFD at end, little-endian) and PLAN/ADR-0001 (triggerer container).
8. **Cleanup:** AIS CSV parsing is duplicated in `adapters/dma.py` and `algorithms/ais_dma.py`; unify. Remove merged worktrees under `.claude/worktrees/` and the merged stream branches.
9. **Not done:** per-Detection acquisition time for validation; ML dataset export; first real run on the Great Belt.
