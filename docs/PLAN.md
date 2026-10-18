# Build Plan

Contract first, then components in parallel against fakes, each gated by its own tests before it integrates. Vocabulary: `GLOSSARY.md`. Decisions: `docs/adr/`.

## 1. Containers

| Container | Purpose | Talks to (protocol) |
|---|---|---|
| `airflow-*` (api-server, scheduler, dag-processor, triggerer for the deferrable operators) | Schedules DAGs; thin, never processes data (ADR-0001, 0002) | `pipeline-api` (HTTP/JSON), `airflow-db` (SQL) |
| `airflow-db` | Airflow metadata only | — |
| `pipeline-api` | FastAPI. Runs the stages as async jobs; owns all geospatial dependencies and all business logic | `postgis` (SQL), `objectstore` (S3), CDSE and DMA (HTTPS, via adapters), Prometheus scrapes `/metrics` |
| `postgis` | Application database: Regions, Scenes, Detections, AIS Positions, Validations, Jobs | — |
| `objectstore` | Object store: cropped Scene rasters, Chips, dataset exports | — |
| `prometheus` | Scrapes pipeline metrics | `pipeline-api` |
| `grafana` | Dashboards provisioned from code | `prometheus`, `postgis` (read-only user) |
| `fake-sources` (dev/CI only) | Stands in for CDSE catalogue + S3 and the DMA bucket, serves fixtures | — |

Not containers: the Detector, Validator and adapters are Python modules inside `pipeline-api`'s image, behind Protocols, so they are unit-testable without any container.

## 2. Contracts (written first, in `contracts/`)

1. **`openapi.yaml`**: hand-written HTTP API (below). `pipeline-api` must conform; Airflow DAG tests are built from it.
2. **`schema.sql` + Alembic migration `0001`**: tables, enums (scene status, match label), spatial indexes.
3. **`storage.md`**: object store key layout (`scenes/{scene_id}/sigma0.npy`, `geo.npz`, `meta.json`, `preview.png`, `chips/{detection_id}.png`, `datasets/{version}/manifest.json`).
4. **`protocols.py`**: `CatalogueClient`, `SceneFetcher`, `AisSource`, `Detector`, `Validator`, `ObjectStore`, plus the value types they exchange.
5. **`metrics.md`**: metric names and labels.
6. **`config.schema.json`** and example `region.greatbelt.yaml`: Region polygon, CFAR parameters, match gate.

### HTTP API sketch (`/v1`)

- `POST /stages/discover` `{region_id}` → `202 Job`. Registers new Scenes (status `discovered`).
- `POST /stages/fetch` `{scene_id}` → `202 Job`. Downloads and crops to the Region (`fetched`).
- `POST /stages/detect` `{scene_id, detector?}` → `202 Job`. Writes Detections and Chips (`detected`).
- `POST /stages/ais-load` `{region_id, date}` → `202 Job`. Loads that day's AIS Positions for the Region.
- `POST /stages/validate` `{scene_id}` → `202 Job`. Writes Validations (`validated`).
- `GET /jobs/{job_id}` → `{status: queued|running|succeeded|failed, error?, result?}`. Same stage and same input returns the existing Job (idempotent).
- `GET /scenes?status=&ais_ready=` and `GET /scenes/{scene_id}`: the validation DAG asks which Scenes are ready.
- `GET /scenes/{scene_id}/detections`: GeoJSON FeatureCollection.
- `GET /healthz`, `GET /metrics`.

Scene status machine: `discovered → fetched → detected → validated`, or `failed` with `failed_stage` and `error`. Scene id is the Sentinel product name.

### DAGs

- `scene_pipeline` (daily): discover → map over new Scenes: fetch → detect.
- `ais_load` (daily): ais-load for the newest available day; updates an Asset.
- `validation` (on Asset): `GET /scenes?status=detected&ais_ready=true` → map: validate.

## 3. Build order and test gates

**Step 0 (serial):** repo scaffolding (`uv`, Makefile, lint, type check), all six contracts, CI skeleton. Gate: `openapi.yaml` validates, `schema.sql` applies to an empty PostGIS, protocols type-check.

**Step 1 (parallel):** each stream builds only against the contracts and fakes.

| Stream | Builds | Gate (must pass before integration) |
|---|---|---|
| A. Database | Alembic migrations, `postgis` container | migrate up/down; constraint and spatial-query tests |
| B. Fixtures | `fake-sources`, small real Scene crop, small AIS slice | fixtures load; fake endpoints match the real response shapes |
| C. Algorithms | CFAR `Detector`, land mask, `Validator`, AIS file parser | unit tests on synthetic rasters (injected ships, known tracks) plus one real fixture |
| D. API skeleton | `pipeline-api` job framework, endpoints with stubbed stages | schemathesis run against `openapi.yaml`; idempotency and failure-state tests |
| E. Adapters | CDSE client, DMA client, S3 `ObjectStore`, **GRD partial-read spike done (ADR-0004); next: strip-offset parsing, calibration, retries** | adapter tests against `fake-sources`; spike result written up (ADR if it changes the design) |
| F. DAGs | the three DAGs | DAG import test; DAG runs against a stub of the API built from `openapi.yaml` |
| G. Observability | `/metrics`, Prometheus config, Grafana dashboards | `promtool check`; dashboard JSON lint; metric names match `metrics.md` |

**Step 2 (serial):** swap stubs for real implementations one stage at a time (fetch, then detect, then ais-load, then validate), re-running the gate after each swap.

**Step 3:** end-to-end test on fixtures in compose (CI), then a first real run on the Great Belt.

## 4. Things needing your input later

- A (free) CDSE account for the real data; credentials stay in an uncommitted `.env`.
- One real Sentinel-1 scene to cut the fixture from.
