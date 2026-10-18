# SAR Ship Locator

An automated pipeline that watches the Great Belt (between the Baltic Sea and the Kattegat) in Sentinel-1 radar imagery. New scenes are ingested as they are published, ships are detected (CA-CFAR), Detections are checked against Danish AIS broadcasts, and everything lands in PostGIS. The whole system starts with one `docker compose` command.

The project is built to showcase the pipeline and its structure: contract-first design, one test gate per component, orchestration, observability and CI.

## Quick start

Needs Docker and a free [Copernicus Data Space](https://dataspace.copernicus.eu) account.

1. Create S3 keys at https://eodata-s3keysmanager.dataspace.copernicus.eu/ (the secret is shown once).
2. `./scripts/init_env.sh` creates `.env` with generated secrets; put the two keys in `CDSE_S3_ACCESS_KEY` and `CDSE_S3_SECRET_KEY`.
3. `docker compose up -d --build --wait`. The first start downloads the OpenStreetMap land polygons (~0.9 GB, once) and queries the public Overpass API for bridges, wind turbines and platforms (can take a few minutes if Overpass is busy); a later start takes about 30 seconds.

| Service | URL |
|---|---|
| Airflow (DAGs: scene_pipeline, ais_load, validation) | http://localhost:8080 (admin password is printed in the api-server logs) |
| Grafana (pipeline health, detection maps, dark-ship candidates, chip gallery, match analysis) | http://localhost:3000 |
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
- Bridges, wind turbines and offshore platforms are masked like land; without that they were a third of the false alarms. Unmatched, confident, ship-sized open-water Detections appear in the `dark_ship_candidates` view (candidates only, not proof).

## Training data

Every validated Detection keeps its Chip and an AIS-derived label. `docker compose run --rm pipeline-api python -m pipeline.dataset v3` exports an immutable, versioned dataset (manifest plus a `chips.npz` bundle). The labels are weak (an AIS match means a ship was there; no match means unknown), and examples are split by scene so near-identical neighbouring chips never leak between train and test.

## Review website

A static page (Leaflet, no build step) shows one Scene at a time: the VV radar image as an overlay, Detections coloured by validation outcome with their Chips, the dark-ship candidates, AIS vessels at acquisition time with their tracks (name, length, type), and filters. Export the data and serve it:

```sh
docker compose run --rm -v $PWD/site/data:/out pipeline-api python -m pipeline.site_export --out /out
cd site && python3 -m http.server 8099      # http://localhost:8099
```

## What the data showed

Matching ships against AIS looked easy and was not. The first real run put matched ships a median 131 m (p90 295 m) from their AIS position, growing with speed. A fit over the moving ships showed it was not timing:

- **Doppler displacement.** A SAR image places a moving target by its Doppler shift, so a ship with a speed along the radar's line of sight is displaced along the satellite track by about 60 s times that speed (5 m/s is 300+ m). `fetch` reads the imaging geometry from the product annotation and the Validator moves every AIS track the same way before matching (median 131 to 74 m).
- **Sea height.** The remaining error was a constant 57 m toward the sensor, for every ship in every Scene. The product is geolocated on the ellipsoid and the sea surface is about 42 m above it (the geoid), which shifts a ship by `h / tan(incidence)`. Correcting it brought the median to 52 m (p90 96 m).
- **Antenna position.** AIS reports the antenna, usually aft, while the radar return is centred on the hull: Detections of large ships sat 40-60 m ahead of their AIS point. The antenna distances from the AIS static data move the reference to the hull centre (median 33 m, p90 67 m).
- With the positions fixed the match gate went from 500 to 200 m, which removed most ambiguous matches.

Details, numbers and the things that did not pan out (structure displacement, antenna offset) are in `AGENTS.md`.

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
