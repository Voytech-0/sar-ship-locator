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

Everything runs: `docker compose up -d --build --wait` starts the whole system (PostGIS, migrations, SeaweedFS object store, land polygon init job, pipeline-api, Prometheus, Grafana, Airflow) in about 30 s once the land file exists. The first real run through Airflow ingested real Sentinel-1 scenes end to end. CI has jobs for Python (lint, mypy, tests incl. Docker), schema, `dags/`, and a compose config and image build.

Gate: `make ci` (ruff, strict mypy, about 315 tests, Docker needed); `dags/` is its own uv project (`cd dags && uv sync && uv run pytest`, 39 tests).

## How the pieces fit

- `compose.yaml` (root) includes `deploy/airflow/compose.yaml`. One image (`deploy/pipeline-api/Dockerfile`, with the `land` extra) serves pipeline-api and the one-shot `migrate` (Alembic, also creates the Grafana login role from `GRAFANA_DB_PASSWORD`) and `land-init` (builds the OSM land file into a volume, skipped when present) jobs. `scripts/init_env.sh` generates the secrets; only the Copernicus S3 keys are the user's.
- `src/pipeline/stages/real.py`: the five stage handlers over `Services`; `stages/build.py` builds production `Services` from env (`CDSE_S3_*`, `STORE_*`, `PIPELINE_LAND_POLYGONS`); `api/main.py` is the entry point. Tests inject fakes.
- Between `fetch` and `detect` the raster lives in the object store (`contracts/storage.md`: `sigma0.npy`, `geo.npz` lattice, `meta.json`). `detect` masks land (`LandMask.not_land_mask`), flags near-shore, writes chips, deletes the raster afterwards.
- `fetch` sets `scenes.acquired_at` to the time of the Region window's centre row (the AIS match needs the imaging time of the ships, not the scene start). Per-detection row time is not used yet (about +-5 s across the window, under 50 m for a 15 kn ship).
- Object store: SeaweedFS, because MinIO no longer publishes public images (ADR-0005). Code and env use neutral names (`S3ObjectStore`, `STORE_*`).
- Metrics: `/v1/metrics` (Prometheus scrapes that path), gauges refreshed at scrape time; `/v1/healthz` also checks the object store.

## First real run (2026-10-17)

- Through Airflow: `scene_pipeline` found 2 scenes (15 Oct), fetched and detected them; `ais_load` loaded 12, 13 and 14 Oct (327,844 / 284,499 / 293,752 positions). The 15 Oct scenes correctly stay `detected` until their AIS day is published (ADR-0003).
- I then ingested 12-14 Oct scenes through the API and the `validation` DAG validated all of them automatically. Across 5 scenes with detections: matched distance median 117 m, p90 290 m, max 497 m (gate 500 m).
- On the 14 Oct 05:32:04 scene (the fixture's source): 474 Detections, 94 open water, 12 of those matched to AIS; 77 open-water Detections are unmatched. Whether they are false alarms, vessels without AIS, or both is not yet known; this is the first thing to study (look at the chips of unmatched open-water Detections).
- Startup race handled by design: the first scheduled DAG run fired before the API was ready and recovered through Airflow's retries.

## Bugs found by running it for real (fixed)

- Fixture TIFF was big-endian while real files are little-endian, which hid a fetcher byte-order bug.
- `bytes_downloaded` was shared state on the fetcher, so concurrent fetches (2 API workers) corrupted each other's counts; now per call (regression test).
- Prometheus scraped `/metrics` but the API serves `/v1/metrics`.
- SeaweedFS health check: the S3 gateway opens later than its master; the check now probes the S3 port.

## Results worth knowing

- **Partial download (ADR-0004):** only the VV rows covering the Region are fetched (up to 650 MB per scene, less for partial overlap). Real GRD TIFFs are little-endian with the directory at the end of the file.
- **Land polygons:** `make land` builds `data/land/great-belt.geojson` (53 polygons, ODbL). On the fixture scene real land cuts Detections from 98 to 19: exactly the 3 known ships are open water and 16 are near-shore.
- **Geolocation:** one affine over the full window is off by up to 197 m; the pipeline uses the GCP-based `geolocate`.
- **API:** a succeeded Job dedupes forever on (stage, input) (the `discover` DAG sends a changing `since`). `detect` is allowed only on `fetched` Scenes.
- **Airflow:** 3.3.2, deferrable operators (needs a triggerer). Admin password of the simple auth manager is printed in the api-server logs.
- **Observability:** Grafana dashboards and Prometheus are up in the stack; rate/increase panels still unproven (needs a few minutes of real data).

## Open items

1. **Sea-level sigma0 looks high:** open water median about -12.5 dB, expected about -20 to -25 dB. Probably a windy day, unverified; thermal noise is not subtracted. Matters for CFAR tuning.
2. **Detection quality:** study the 77 unmatched open-water Detections of the 14 Oct scene (chips are in the object store, labels in `validations`); tune `pfa`, minimum cluster size and the near-shore handling with AIS-matched Detections as positives. This is also the start of the ML dataset (export command not written).
3. **ADR-0003 vs API:** a second Detector version cannot run on a `detected` Scene via the API; deferred until the ML layer exists.
4. **Docs:** amend ADR-0004 (IFD at end, little-endian) and PLAN/ADR-0001 (triggerer container; PLAN still lists MinIO/vv.tif in places).
5. **Cleanup:** AIS CSV parsing is duplicated in `adapters/dma.py` and `algorithms/ais_dma.py`; unify. Remove merged worktrees under `.claude/worktrees/` and merged stream branches. `scripts/check_schema.sh` still uses the amd64-only `postgis/postgis:16-3.4`.
6. **Not done:** per-Detection acquisition time for validation; Grafana panels checked against real data; ML dataset export; alerting beyond the two rules.
