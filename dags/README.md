# DAGs

Airflow 3.3 DAGs. They only orchestrate: every stage is a Job on `pipeline-api` (ADR-0001, ADR-0002).
This directory is its **own uv project** because Airflow's pinned dependencies conflict with the pipeline package.

| DAG | Schedule | Does |
|---|---|---|
| `scene_pipeline` | `@daily` | `discover` per Region, then a mapped group per new Scene: `fetch` -> `detect` |
| `ais_load` | `@daily` | `ais-load` for the lagged AIS day(s) per Region; updates the `ais_loaded` Asset |
| `validation` | on `ais_loaded` | `GET /scenes?status=detected&ais_ready=true`, then `validate` per Scene |

## Design choices

- **Deferrable, not blocking.** `StageJobOperator` POSTs the stage, then defers to `JobPollTrigger`, which polls
  `GET /jobs/{id}` in the triggerer. No worker slot is held while a Job runs (minutes for `fetch`), so the stack
  needs the `airflow-triggerer` container. Chosen over a reschedule-mode sensor because it needs no extra task and
  no re-submission logic.
- **Contract semantics.** HTTP 200 (existing queued/running/succeeded Job) and 202 (new Job) are both followed;
  an already-succeeded Job returns immediately, which makes re-runs idempotent.
- **Retries.** 3 retries with exponential backoff (2 min base, 30 min cap). A Job failing with
  `error_retryable: false` raises `AirflowFailException` (no retries). Network errors and 5xx are retried; other
  4xx (unknown id, wrong Scene status, bad body) fail immediately.
- **AIS not published yet.** The API has no "latest available day" endpoint, so run for interval start `D` asks
  for day `D - AIS_LAG_DAYS` (default 3) and the `AIS_RETRY_DAYS` (default 2) days before it; days already loaded
  answer 200 at once. A day still missing after the retries is **skipped, not failed**: the run stays green, no
  Asset event is emitted for it, and the next run asks again. Any other failure fails the run.
- **A failed Scene stays failed.** `scene_pipeline` only handles Scenes that `discover` reports as new. Re-running
  one is a manual "clear" in the Airflow UI (ADR: no automatic infinite loops).

## Development

```sh
uv sync
uv run pytest        # DAG import/structure + end-to-end runs against tests/stub_api.py
uv run ruff check . && uv run mypy
```

`tests/stub_api.py` implements the contract's endpoints with scriptable outcomes and validates every request and
response against `contracts/openapi.yaml`. Tests run DAGs with `dag.test()` (deferred Jobs are resolved in-process)
in a throwaway `AIRFLOW_HOME` with SQLite.

Upgrading Airflow: `python update_constraints.py <version>` copies the official constraints file into
`pyproject.toml` (uv has no `--constraint` for `uv lock`), then `uv lock`. Bump the image tag in
`deploy/airflow/Dockerfile` and `compose.yaml` to match.

## Deploy

`deploy/airflow/compose.yaml` runs the stack (api-server, scheduler, dag-processor, triggerer, own Postgres,
LocalExecutor). Required env: `AIRFLOW_DB_PASSWORD`, `AIRFLOW_FERNET_KEY`, `AIRFLOW_JWT_SECRET`,
`AIRFLOW_API_SECRET_KEY`; optional `PIPELINE_API_URL` (default `http://pipeline-api:8000/v1`).
