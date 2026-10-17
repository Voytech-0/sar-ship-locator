# SAR Ship Locator

Automated pipeline that detects ships in Sentinel-1 radar scenes of the Great Belt, validates them against Danish AIS, and stores the results. Built to be deployed with `docker compose`, tested per component, and developed contract-first.

Status: scaffolding and contracts done; components not yet built.

## Where to look

- `GLOSSARY.md`: the project vocabulary (Region, Scene, Detection, ...).
- `docs/PLAN.md`: containers, how they communicate, build order and test gates.
- `docs/adr/`: why Airflow, why an async job API, why validation is delayed.
- `contracts/`: the API, database, storage, metrics and interface definitions that all components build against.

## Layout

| Path | Contents |
|---|---|
| `contracts/` | OpenAPI spec, SQL schema, Python Protocols, config schema |
| `src/pipeline/` | the pipeline package and its FastAPI service |
| `dags/` | Airflow DAGs (thin: they only call the API) |
| `deploy/` | Dockerfiles, compose files, Prometheus and Grafana config |
| `migrations/` | Alembic migrations |
| `tests/` | tests, with `tests/fixtures/` for committed sample data |
| `scripts/` | helper scripts used by `make` and CI |

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
make install        # create the virtualenv
make ci             # lint, typecheck, test, check-schema
```

Copy `.env.example` to `.env` for credentials (never committed).
