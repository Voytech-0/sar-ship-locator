# SAR Ship Locator

An automated pipeline that watches the Great Belt (between the Baltic Sea and the Kattegat) in Sentinel-1 radar imagery. New scenes are ingested as they are published, ships are detected (CA-CFAR), Detections are checked against Danish AIS broadcasts, and everything lands in PostGIS. The whole system starts with one `docker compose` command.

The project is built to showcase the pipeline and its structure: contract-first design, one test gate per component, orchestration, observability and CI.

## Quick start

Needs Docker and a free [Copernicus Data Space](https://dataspace.copernicus.eu) account.

1. Create S3 keys at https://eodata-s3keysmanager.dataspace.copernicus.eu/ (the secret is shown once).
2. `./scripts/init_env.sh` creates `.env` with generated secrets; put the two keys in `CDSE_S3_ACCESS_KEY` and `CDSE_S3_SECRET_KEY`.
3. `docker compose up -d --build --wait`. The first start downloads the OpenStreetMap land polygons (~0.9 GB, once); a later start takes about 30 seconds.

| Service | URL |
|---|---|
| Airflow (DAGs: scene_pipeline, ais_load, validation) | http://localhost:8080 (admin password is printed in the api-server logs) |
| Grafana (pipeline health, detections map) | http://localhost:3000 |
| Pipeline API (OpenAPI in `contracts/openapi.yaml`) | http://localhost:8000/v1/scenes |
| Prometheus | http://localhost:9090 |

The DAGs run daily by themselves. To run one now: `docker compose exec airflow-scheduler airflow dags trigger scene_pipeline`.

## How it works

```
CDSE catalogue ──► scene_pipeline (daily): discover ─► fetch ─► detect ──┐
CDSE S3 (VV rows of the Region only)                                     ▼
DMA AIS archive ──► ais_load (daily) ─────────────────────────────► PostGIS ◄── validation (when AIS covers a Scene)
                                                                         ▲
Airflow only orchestrates; pipeline-api runs every stage as an async job ┘   Prometheus ─► Grafana
```

- Detections appear within minutes of a Scene being published. AIS archives arrive about 3 days late, so Validation (Matched / Unmatched / Ambiguous) runs later, triggered by an Airflow Asset (ADR-0003).
- Only the rows of the VV band covering the Region are downloaded, about 400 MB of an 883 MB file (ADR-0004).

## Where to look

- `GLOSSARY.md`: the project vocabulary (Region, Scene, Detection, ...).
- `docs/PLAN.md` and `docs/adr/`: the build plan and why each major decision was made.
- `contracts/`: the API, database, storage, metrics and interface definitions every component builds against.
- `AGENTS.md`: current state, results and open work.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
make install     # virtualenv
make ci          # lint, typecheck, tests, schema check
make land        # build the land polygon file locally (the compose stack does this itself)
```

`dags/` is its own uv project (Airflow's pinned dependencies conflict with the pipeline's): `cd dags && uv sync && uv run pytest`.

Data: Copernicus Sentinel-1 (ESA), AIS from the Danish Maritime Authority, land polygons (c) OpenStreetMap contributors (ODbL).
