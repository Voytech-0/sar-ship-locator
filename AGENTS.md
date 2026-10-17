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

## Study of the unmatched open-water Detections (2026-10-17)

First real run, 14 Oct scene (CA-CFAR v1, pfa 1e-6, min 3 px): 474 Detections, 94 open water, 12 AIS-matched, 77 unmatched. Looking at the chips of the unmatched ones:

- Most of the strong unmatched Detections were the Great Belt Bridge (long diagonal deck streaks of 150-290 m and pylon dots at lat 55.34, lon 10.94-11.08): 63 of 181 remaining unmatched on the three studied scenes were on the bridge corridor.
- Most weak ones (confidence under 0.5, median length 36 m) were sharp diagonal sea-state edges (the border between wind-sheltered calm water and rougher water) and speckle; they were also clustered (half had 2+ neighbours within 300 m).
- What remains are small bright dots in groups (36-60 m: turbines, buoys or small boats) and a handful of large ship-shaped targets without AIS: the dark-ship candidates.

Offline experiments (3 scenes, 12/13/14 Oct, real land): the edge-contrast idea did not separate matched from unmatched (median 1.34 vs 1.30) and was dropped. What worked: (a) a structures mask: OSM bridges, wind turbines and offshore platforms added to the land file as buffered polygons (80/75/100 m), unmatched 461 to 338 with no matched ship lost; (b) stricter detector settings: v2 = pfa 1e-10 and min 6 px keeps 47 of 50 matched ships and gives unmatched 109 with the structures mask (a 76 percent cut vs 461). Grid in the table of the experiment: pfa 1e-8..1e-12 x min 4..10 px; 1e-10/6 is the chosen knee (3 scenes only, so treat it as a starting point, not a tuned result).

Re-run on a fresh stack with v2 and the structures mask: 14 Oct scene 474 to 157 Detections, 77 to 7 unmatched open-water; the `dark_ship_candidates` view (unmatched, open water, length 100 m or more, confidence 0.9 or more) lists 6 targets, all from the 12 Oct scene, matching the ship-like chips found by eye. A candidate is not proof (small vessels, AIS gaps, clutter remain possible).

Detector `version` now labels a parameter set and comes from the Region config (`detector.version`, currently "2"); bump it when parameters change.

## Dataset export (2026-10-17)

`python -m pipeline.dataset <version>` (module `pipeline/dataset.py`) writes `datasets/<version>/manifest.json` and `chips.npz` to the object store. Labels are weak AIS labels (matched = 1, unmatched = 0, ambiguous left out by default); versions are immutable; splits are allocated over whole scenes (`assign_splits`: 70/15/15, floor with a minimum of one scene per split from three scenes up). A first attempt hashed each scene independently and, with only 5 scenes, put all of them in `train`; the allocation fixed that. First real export `v2`: 747 examples from 5 scenes (train 679, validation 39, test 29; 69 matched in all). Dataset `v1` in the local object store has the flawed all-train split and can be deleted.

## Grafana (2026-10-17)

`deploy/grafana/build_dashboards.py` generates both dashboards (regenerate with `uv run python deploy/grafana/build_dashboards.py`, then restart Grafana). All panel queries were checked through Grafana's query API against real data. The Detections dashboard has: per-scene counts, label split, a map of all Detections coloured by validation outcome, a map and table of the dark-ship candidates, and a chip gallery with Label and Shore filters. Chips are PNGs in the object store, which a browser cannot reach, so pipeline-api serves them at `GET /v1/detections/{id}/chip` and the tables show them as image cells; the constant dashboard variable `api_url` (default http://localhost:8000) is where the browser finds the API. The default Grafana basemap is CARTO, which now returns watermarked "API key required" tiles, so the maps use OpenStreetMap.

## Exclusion zone and the match-analysis dashboard (2026-10-17)

`land_mask.exclusion_m` (Region config, now 100): Detections within 100 m of land are ignored like land (quays, piers, moored boats); `near_shore_m` (300) still only flags. `PolygonLandMask` removes the zone from the detection mask and classifies positions inside it as ON_LAND. Offline on three scenes: near-shore Detections 590 to 31, matched ships 47 to 47. Re-run on the live stack: total Detections 827 to 241, near-shore about 590 to 48.

Grafana has a third dashboard, Match analysis (`sar-matches`): outcome counts, match rate and median/p90 distance, a histogram of match distances, outcome by length and by confidence, median distance by AIS speed, per-scene table, a map of matches coloured by distance, and review tables with chips (furthest matches, ambiguous, longest unmatched). Variables: Shore (default open water) and Detector version. First readings on real data: Detections of 100 m or longer are matched 73-100 percent of the time, below 100 m only 9-15 percent; match distance grows with vessel speed (median 50 m at 0-3 kn, 120-180 m above), which points at the per-Detection acquisition time not being used yet (open item).

## Review website (2026-10-17)

`site/index.html` (Leaflet, no build step) shows a validated Scene: VV preview overlay (toggle, opacity), Detections by validation outcome with chips, dark-ship candidates, AIS vessels at acquisition time with +-10 min tracks, match links, filters, summary. Static: `site/data/` is git-ignored and made by `python -m pipeline.site_export` (`make site`; from Docker: `docker compose run --rm -v $PWD/site/data:/out pipeline-api python -m pipeline.site_export --out /out`), then `cd site && python3 -m http.server 8099` (8080 is Airflow). `detect` now writes `scenes/{id}/preview.png` + `preview.json` (30 m lat/lon grid, kept after the raster is deleted; storage.md updated). VV only for now: an RGB VV/VH ratio composite needs a VH fetch (contract + ADR-0004 change, not decided). Only the 14 Oct Scene was re-ingested with previews. Not done: GitHub Pages deploy, committed demo snapshot, preview size check on a full-overlap Scene (this one is a thin strip, 224 KB; cap is 5 MB).

## Bugs found by running it for real (fixed)

- Fixture TIFF was big-endian while real files are little-endian, which hid a fetcher byte-order bug.
- `bytes_downloaded` was shared state on the fetcher, so concurrent fetches corrupted each other's counts; now per call (regression test).
- `fetch` computed the Region window time from the already rewritten `scenes.acquired_at`, so every retry shifted it again; the catalogue time now lives in `scenes.acquired_start` (migration 0003) and fetch always derives from it (regression test).
- Manually triggered Airflow runs have no data interval and may have no logical date (only `run_after`); `ais_load` crashed and `scene_pipeline`'s `since` template would have; both now fall back correctly (tests). Scheduled runs were never affected.
- Prometheus scraped `/metrics` but the API serves `/v1/metrics`.
- SeaweedFS health check: the S3 gateway opens later than its master; the check now probes the S3 port.
- Overpass answers 406 to anonymous clients: the loader sends a descriptive User-Agent and retries when the server is busy.

## Results worth knowing

- **Partial download (ADR-0004):** only the VV rows covering the Region are fetched (up to 650 MB per scene, less for partial overlap). Real GRD TIFFs are little-endian with the directory at the end of the file.
- **Land polygons:** `make land` builds `data/land/great-belt.geojson` (53 OSM land polygons plus 650 structure polygons from Overpass, ODbL; needs network on first build). On the fixture crop real land cuts Detections from 98 to 19 with the old detector settings.
- **Geolocation:** one affine over the full window is off by up to 197 m; the pipeline uses the GCP-based `geolocate`.
- **API:** a succeeded Job dedupes forever on (stage, input) (the `discover` DAG sends a changing `since`). `detect` is allowed only on `fetched` Scenes.
- **Airflow:** 3.3.2, deferrable operators (needs a triggerer). Admin password of the simple auth manager is printed in the api-server logs.
- **Observability:** Grafana dashboards and Prometheus are up in the stack; rate/increase panels still unproven (needs a few minutes of real data).

## Open items

1. **Sea-level sigma0 looks high:** open water median about -12.5 dB, expected about -20 to -25 dB. Probably a windy day, unverified; thermal noise is not subtracted.
2. **Detection quality, next steps:** the 90 unmatched open-water Detections in the 12 Oct scene deserve a look (a large rough-sea or wind-shadow area?); GO-CFAR or a texture test for sea-state edges; thresholds of `dark_ship_candidates` are a starting point; add the candidates to a Grafana panel; the ML dataset export exists (below); train a first classifier on it.
3. **Re-processing:** a `detect` cannot be re-run on a `detected` Scene via the API, and the raster is deleted after detect, so changing the detector means re-fetching (ADR-0003 vs API; deferred until the ML layer exists). Local dev: wipe the volumes (`docker volume rm sar_pg-data sar_store-data`).
4. **Docs:** amend ADR-0004 (IFD at end, little-endian) and PLAN/ADR-0001 (triggerer container; PLAN still mentions MinIO/vv.tif in places).
5. **Cleanup:** AIS CSV parsing is duplicated in `adapters/dma.py` and `algorithms/ais_dma.py`; unify. Remove merged worktrees under `.claude/worktrees/` and merged stream branches. `scripts/check_schema.sh` still uses the amd64-only `postgis/postgis:16-3.4`. One schemathesis subtest failed once in a full-suite run and did not reproduce in 5 reruns (watch for flakiness).
6. **Not done:** per-Detection acquisition time for validation (match distance grows with vessel speed, so this is the likely biggest accuracy gain); Grafana panels checked against real data; alerting beyond the two rules.
