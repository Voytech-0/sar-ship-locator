# AGENTS.md

Guidance and current state for agents working in this repo. Vocabulary is in `GLOSSARY.md`, decisions in `docs/adr/`, the build plan in `docs/PLAN.md`, interfaces in `contracts/`.

Measurements and their history are in `docs/studies/`.

## Project

Automated pipeline: Sentinel-1 GRD scenes of the Great Belt are ingested daily, ships are detected (CA-CFAR), Detections are validated against Danish AIS (about 3 days late) and stored in PostGIS. The pipeline and its structure are the portfolio showpiece, so tests per component and contract-first work matter more than detection accuracy.

## Rules

- No `Co-Authored-By` or other attribution lines in commits or PR descriptions.
- Never print or commit secrets. Credentials live in the uncommitted `.env` (S3 keys only are needed; the CDSE username and password are not).
- Build against `contracts/`. Do not change a contract to suit one component: record the issue below and get a decision.
- `make ci` runs lint, mypy, tests and the schema check. Tests that need Docker skip when it is missing.
- CI: all four jobs (python, schema, dags, compose) passed on GitHub on first run (PR #1, 2026-10-18).

## Layout

`contracts/` API, schema, protocols, config, metrics, storage. `src/pipeline/` package. `dags/` Airflow (own uv project). `deploy/` Dockerfiles and compose pieces. `migrations/` Alembic. `tests/` including `tests/fixtures/`. `docs/` plan and ADRs.
## Current state (2026-11-11)

Everything runs: `docker compose up -d --build --wait` starts the whole system (PostGIS, migrations, SeaweedFS, land init job, pipeline-api, Prometheus, Grafana, Airflow) in about 30 s once the land file exists. Real Sentinel-1 Scenes went through Airflow end to end. The local stack was rebuilt from scratch on 2026-11-12: 49 Scenes since 12 Oct went through the real `fetch` (VV, VH, noise LUT, geometry), `detect`, AIS backfill and `validate` with no hand patching, and the 9 original Scenes reproduce the earlier numbers exactly ([clean-ingest](docs/studies/clean-ingest.md)).

Gate: `make ci` (ruff, strict mypy, 400 tests, 4 skipped without RUN_LIVE, Docker needed); `dags/` is its own uv project (`cd dags && uv sync && uv run pytest`, 43 tests).

## How the pieces fit

- `compose.yaml` (root) includes `deploy/airflow/compose.yaml`. One image (`deploy/pipeline-api/Dockerfile`, with the `land` extra) serves pipeline-api and the one-shot `migrate` (Alembic, also creates the Grafana login role from `GRAFANA_DB_PASSWORD`) and `land-init` (builds the OSM land file into a volume, skipped when present) jobs. `scripts/init_env.sh` generates the secrets; only the Copernicus S3 keys are the user's.
- `src/pipeline/stages/real.py`: the five stage handlers over `Services`; `stages/build.py` builds production `Services` from env (`CDSE_S3_*`, `STORE_*`, `PIPELINE_LAND_POLYGONS`); `api/main.py` is the entry point. Tests inject fakes.
- Between `fetch` and `detect` the raster lives in the object store (`contracts/storage.md`: `sigma0.npy`, `geo.npz` lattice, `meta.json`). `fetch` stores VV and VH (`sigma0_vh.npy`, ADR-0007); `detect` masks land (`LandMask.not_land_mask`), flags near-shore, writes chips, deletes the raster afterwards.
- `fetch` sets `scenes.acquired_at` to the time of the Region window's centre row (the AIS match needs the imaging time of the ships, not the scene start). Per-detection row time is not used yet (about +-5 s across the window, under 50 m for a 15 kn ship).
- Object store: SeaweedFS, because MinIO no longer publishes public images (ADR-0005). Code and env use neutral names (`S3ObjectStore`, `STORE_*`).
- Metrics: `/v1/metrics` (Prometheus scrapes that path), gauges refreshed at scrape time; `/v1/healthz` also checks the object store.

## Detector and validation, current

Region config parameters; history and evidence in `docs/studies/`.

- Structures mask: OSM bridges, turbines, platforms as buffered polygons (80/75/100 m) in the land file ([unmatched-detections](docs/studies/unmatched-detections.md)).
- Exclusion zone `land_mask.exclusion_m` 100 m ignored like land, `near_shore_m` 300 only flags ([match-geometry](docs/studies/match-geometry.md)).
- CA-CFAR on VV: pfa 1e-10, min 6 px; `length_bias_m` 20; floors `min_length_m` 30 and `min_confidence` 0.7 on the corrected length ([detector-versions](docs/studies/detector-versions.md)).
- VH confirmation `min_vh_contrast_db` 24 (ADR-0007); `fetch` subtracts thermal noise (annotation noise LUT), detector v6.
- Validation: AIS tracks moved by Doppler displacement, sea-level height 42 m (`validation.sea_level_height_m`) and antenna-to-hull-centre offset before matching; `validation.gate_m` 200 ([match-geometry](docs/studies/match-geometry.md)).
- `detector.version` (now "6") labels a parameter set: bump it in the Region config whenever a detector parameter changes; `detect` then makes a new Job and the old version's Detections stay. `validate` validates each version on its own; stage requests take `force: true` for a rerun.

## Tools

- Dataset export: `python -m pipeline.dataset <version>` writes `datasets/<version>/manifest.json` and `chips.npz` to the object store; weak AIS labels, immutable versions, splits over whole Scenes.
- Grafana: `uv run python deploy/grafana/build_dashboards.py` regenerates the three dashboards (Detections, Match analysis, pipeline); restart Grafana after. Chips are served by `GET /v1/detections/{id}/chip`; `api_url` variable is where the browser finds the API. Maps use OpenStreetMap (CARTO tiles need a key).
- Review website: `make site` (or the `docker compose run` form in the study file) writes `site/data/`, then `cd site && python3 -m http.server 8099`. Not done: GitHub Pages deploy, committed demo snapshot.
- History of these three: [tooling-history](docs/studies/tooling-history.md). Bugs found by real runs: [bugs-from-real-runs](docs/studies/bugs-from-real-runs.md).

## Results worth knowing

- **Partial download (ADR-0004):** only the VV rows covering the Region are fetched (up to 650 MB per scene, less for partial overlap). Real GRD TIFFs are little-endian with the directory at the end of the file.
- **Land polygons:** `make land` builds `data/land/great-belt.geojson` (53 OSM land polygons plus 650 structure polygons from Overpass, ODbL; needs network on first build). On the fixture crop real land cuts Detections from 98 to 19 with the old detector settings.
- **Geolocation:** one affine over the full window is off by up to 197 m; the pipeline uses the GCP-based `geolocate`.
- **API:** a succeeded Job dedupes forever on (stage, input) (the `discover` DAG sends a changing `since`). `detect` needs a stored raster (see Open items 3).
- **Airflow:** 3.3.2, deferrable operators (needs a triggerer). Admin password of the simple auth manager is printed in the api-server logs.
- **Observability:** Grafana dashboards and Prometheus are up in the stack. On 2026-10-18 every SQL panel (through Grafana's query API) and every Prometheus panel expression returned data on the real stack; the detector-version variable on the Detections and Match dashboards defaults to the newest version.

## Open items

1. **Sea-level sigma0 looks high:** open water median about -12.5 dB in VV for five of the nine Scenes (-21 to -22 dB on the 12 Oct and 15 Oct 05:24 ones), expected -20 to -25 dB. Noise subtraction moved it by only 0.1-0.4 dB; most likely wind-roughened sea, unverified.
2. **Detection quality:** recall against AIS is 100 percent for ships of 30 m and longer on the 9 Scenes, so the open question is false alarms; GO-CFAR or a texture test for sea-state edges; `dark_ship_candidates` thresholds are a starting point; train a first classifier on the dataset export.
3. **Flaky test:** one schemathesis subtest failed once in a full-suite run and did not reproduce. 20 full-suite runs on 2026-11-11 (18 plus 2 redone after Docker had stopped), all 400 passed, not reproduced; `print_blob=True` is now set so the next failure prints its reproduction line. The DB test fixture leaked one 2 GB Docker volume per run (`docker rm -f` without `-v`), fixed; check `docker system df` after test loops.
4. **Not done:** per-Detection row time for validation (about 40 m for a 15 kn ship); four alert rules exist and are unit-tested (`make check-alerts`).
