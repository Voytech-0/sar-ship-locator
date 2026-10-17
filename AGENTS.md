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

Step 0 (contracts, scaffolding) is on `main`. Step 1 streams are done, each committed on its own branch in a worktree under `.claude/worktrees/`. **Nothing is merged and no branch is pushed.**

| Stream | Branch | Commit | Gate |
|---|---|---|---|
| A Database | `stream/a-database` | 3c36d46 | `pytest tests/db`: 62 passed |
| B Fixtures, fake-sources | `stream-b-fixtures` | 68c3106 | `pytest tests/fixtures_check`: 19 passed |
| C Algorithms | `stream/c-algorithms` | 6f3a9eb | `pytest tests/algorithms`: 57 passed with contracts tests |
| D API skeleton | `stream/d-api` | b1176bc | `pytest tests/api`: 60 passed plus 12 schemathesis subtests |
| E Adapters | `stream/e-adapters` | 4822822 | `pytest tests/adapters`: 54 passed, 4 live tests skipped (ran live once) |
| F DAGs | `stream/f-dags` | 2609dc8 | from `dags/`: 38 passed |
| G Observability | `stream/g-observability` | 3a02292 | `pytest tests/observability`: 50 passed |

Worktree branch names (the real git refs) are `worktree-agent-<id>`; check `git worktree list`.

## Results worth knowing

- **Partial download (ADR-0004):** only the VV rows covering the Region are fetched with ranged GETs: 399 MB in 9 requests, 18 s, for the whole Great Belt window (full VV file is 883 MB). The TIFF's directory (IFD) is at the END of the file, so the header read must include the tail. ADR-0004 does not say this yet.
- **Validation works on real data:** with the fixture scene, three open-water ships each matched a real AIS vessel at 120 m, 118 m and 227 m after interpolating to the row time. A bright spot on an island is a deliberate false alarm only a land mask removes. Labels are weak: matched means near an AIS track.
- **Live DMA AIS:** latest day was 2026-10-14 on 2026-10-17; one day is 293,752 positions, 168 vessels, loaded in 59 s.
- **API job idempotency:** a succeeded Job dedupes forever on (stage, input). Restarts fail interrupted Jobs as retryable (single API instance assumed).
- **Airflow:** 3.3.2, deferrable operators (needs a triggerer container). `ais_load` requests day D-3 and two earlier days, and skips (stays green) when a day is unpublished.
- **Observability:** Grafana dashboards load and SQL panels work against a live stack; rate/increase panels returned no data in the short test and are unproven.

## Open decisions and contract issues (need an answer before merge)

1. **Geolocation (E, affects C):** one affine over the whole 7481 x 5158 px window is off by up to 19.7 px (197 m); tiles of 1000 px or less give under 1.6 px. Proposed: add `geolocate(rows, cols) -> (lon, lat)` to `SceneRaster`, keep `transform` as a coarse approximation. Matters because the match gate is 500 m.
2. **`LandMask` (C):** `water_mask` excludes the near-shore band, which contradicts flagging near-shore ships. Proposed: `detect` uses a new `not_land_mask()` (land only) then flags with `classify()`; add the method to the protocol.
3. **`DetectionCandidate.pixel_box`:** implemented as half-open `(row0, col0, row1, col1)`; write that into the contract.
4. **`GET /scenes` returns 422** on bad parameters; add the response to `openapi.yaml`.
5. **`discover` idempotency (D, check F):** the DAG must send a changing `since` (e.g. data interval start) or later runs return the first run's result. Not yet checked in F's DAG.
6. **ADR-0003 vs API:** `detect` is allowed only on `fetched` Scenes, so a second Detector version cannot run on a `detected` Scene via the API. Recommended: defer until the ML layer exists.
7. **Bytes downloaded (E):** `FetchResult.bytes_downloaded` has no source because `SceneFetcher` returns only a raster; `last_diagnostics` holds it today.
8. **`ObjectStore.get`** raises `FileNotFoundError` for missing keys; undocumented in the contract.
9. **Sea-level sigma0 looks high (E):** open water median about -12.5 dB, expected about -20 to -25 dB. Formula `DN^2 / sigma0_LUT^2` looks right; possibly a windy day. Cross-check against SNAP or another calibrated source before tuning CFAR. Thermal noise is not subtracted.

## Merge and wiring checklist

- Add dev dependencies `types-shapely`, `scipy-stubs`, `boto3-stubs` (or mypy overrides) so `make typecheck` passes; C and E carry `import-untyped` workarounds to remove.
- `scripts/check_schema.sh` still uses the amd64-only `postgis/postgis:16-3.4`; switch to `deploy/postgis` or replace by `tests/db`.
- Compose: `pipeline-api` (`PIPELINE_DATABASE_URL`, port 8000, after migration), PostGIS, MinIO with bucket `sar`, `fake-sources`, Prometheus, Grafana (mounts and pinned images in stream G's notes), Airflow (merge `deploy/airflow/compose.yaml`, same network as `pipeline-api`).
- Grafana needs a login role: `CREATE ROLE grafana LOGIN PASSWORD '...' IN ROLE grafana_ro`, `GRAFANA_DB_PASSWORD`, database named `sar`.
- The migration job needs both `migrations/` and `contracts/` (0001 reads `contracts/schema.sql`).
- `.env.example`: add `AIRFLOW_DB_PASSWORD`, `AIRFLOW_FERNET_KEY`, `AIRFLOW_JWT_SECRET`, `AIRFLOW_API_SECRET_KEY`, `GRAFANA_DB_PASSWORD`.
- CI: add jobs for `dags/` (`uv sync && uv run pytest` inside it) and the Docker-based tests; extend mypy to new paths.
- Wire the API: stream G metrics helpers and `render()` at `/metrics`; object-store health check; real stage handlers replacing the stubs one at a time (fetch, detect, ais-load, validate), re-running the gate after each.
- Amend ADR-0004 (IFD at end of file) and the plan/ADR-0001 (triggerer container).
- Duplicate AIS CSV parsing exists in `adapters/dma.py` and `algorithms/ais_dma.py`; unify after merge.
- Still missing: OSM land polygons loader; CFAR checks on a real scene; Great Belt polygon is a rough box.
