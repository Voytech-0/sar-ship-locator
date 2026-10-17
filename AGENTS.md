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

Steps 0 and 1 are done and merged into local `main` (not pushed). The seven stream branches (`stream/a-database`, `stream-b-fixtures`, `stream/c-algorithms`, `stream/d-api`, `stream/e-adapters`, `stream/f-dags`, `stream/g-observability`) are fully merged; their worktrees live under `.claude/worktrees/` (gitignored) and can be removed.

Gate on the merged tree: ruff and strict mypy clean; `uv run pytest` 302 passed, 4 live tests skipped; `dags/` 39 passed (run `uv sync && uv run pytest` inside `dags/`). CI has never run on GitHub.

Resolved after merge: contract changes applied (`SceneRaster.geolocate` and `bytes_downloaded`, `LandMask.not_land_mask`, half-open `pixel_box`, `ObjectStore.get` raises `FileNotFoundError`, 422 on `GET /scenes`); type stubs added and the temporary mypy pragmas removed; `discover` now sends a changing `since` (data interval start minus 2 days) with a regression test.

## Results worth knowing

- **Partial download (ADR-0004):** only the VV rows covering the Region are fetched with ranged GETs: 399 MB in 9 requests, 18 s, for the whole Great Belt window (full VV file is 883 MB). The TIFF directory (IFD) is at the END of the file, so the header read must include the tail; ADR-0004 does not say this yet.
- **Validation works on real data:** with the fixture scene, three open-water ships each matched a real AIS vessel at 120 m, 118 m and 227 m after interpolating to the row time. A bright spot on an island is a deliberate false alarm only a land mask removes. Labels are weak: matched means near an AIS track.
- **Live DMA AIS:** latest day was 2026-10-14 on 2026-10-17; one day is 293,752 positions, 168 vessels, loaded in 59 s.
- **Geolocation:** one affine over the full window is off by up to 197 m; tiles of 1000 px or less give under 1.6 px. The fetcher must supply `SceneRaster.geolocate` and the Detector must use it.
- **API:** a succeeded Job dedupes forever on (stage, input). Restarts fail interrupted Jobs as retryable (single API instance assumed). `detect` is allowed only on `fetched` Scenes (see open item 2).
- **Airflow:** 3.3.2, deferrable operators (needs a triggerer container). `ais_load` requests day D-3 and two earlier days, and skips (stays green) when a day is unpublished.
- **Observability:** Grafana dashboards load and SQL panels work against a live stack; rate/increase panels returned no data in the short test and are unproven.

## Open items

1. **Sea-level sigma0 looks high:** open water median about -12.5 dB, expected about -20 to -25 dB. Formula `DN^2 / sigma0_LUT^2` looks right; possibly a windy day. Cross-check against SNAP or another calibrated source before tuning CFAR. Thermal noise is not subtracted.
2. **ADR-0003 vs API:** a second Detector version cannot run on a `detected` Scene via the API. Deferred until the ML layer exists.
3. **Wire the real stages** into the API replacing the stubs, one at a time (fetch, detect, ais-load, validate), re-running the gate after each. The detect stage must use `LandMask.not_land_mask()` as `raster.water_mask`, then flag with `classify()`, and pass `geolocate` through.
4. **Compose stack:** `pipeline-api` (`PIPELINE_DATABASE_URL`, port 8000, after migration), PostGIS (`deploy/postgis`), MinIO with bucket `sar` seeded for dev from `tests/fixtures/s3/`, `fake-sources`, Prometheus, Grafana (mounts and pinned images in `deploy/prometheus`, `deploy/grafana`), Airflow (merge `deploy/airflow/compose.yaml`, same network as `pipeline-api`). Grafana needs a login role: `CREATE ROLE grafana LOGIN PASSWORD '...' IN ROLE grafana_ro`, `GRAFANA_DB_PASSWORD`, database named `sar`. The migration job needs both `migrations/` and `contracts/`.
5. **`.env.example`:** add `AIRFLOW_DB_PASSWORD`, `AIRFLOW_FERNET_KEY`, `AIRFLOW_JWT_SECRET`, `AIRFLOW_API_SECRET_KEY`, `GRAFANA_DB_PASSWORD`.
6. **CI:** add the Docker-based tests and a `dags/` job; `scripts/check_schema.sh` still uses the amd64-only `postgis/postgis:16-3.4`; switch to `deploy/postgis` or replace by `tests/db`.
7. **API wiring:** stream G metrics helpers and `render()` at `/metrics`; object-store health check in `app.state.health_checks`; feed `FetchResult.bytes_downloaded` from `SceneRaster.bytes_downloaded`.
8. **Docs:** amend ADR-0004 (IFD at end of file) and PLAN/ADR-0001 (triggerer container).
9. **Cleanup:** AIS CSV parsing is duplicated in `adapters/dma.py` and `algorithms/ais_dma.py`; unify. Remove merged worktrees.
10. **Missing:** OSM land polygons loader; CFAR run on a real scene; the Great Belt polygon is a rough box.
