# Orchestrate with Apache Airflow instead of a custom queue

Status: accepted

The pipeline (poll Copernicus, download and crop, detect, load, validate) is orchestrated by Airflow in docker-compose with LocalExecutor. Airflow is the most widely used orchestrator and gives scheduling, retries, run history and a UI out of the box, which keeps the project's effort on the pipeline itself.

## Considered Options

- **Custom services plus a queue (Redis/RabbitMQ)**: more engineering to show, but we would rebuild retries, backfills and observability by hand.
- **Dagster / Prefect**: lighter and arguably nicer to write, but less recognised. Revisit if Airflow's footprint (several containers, ~4 GB RAM) becomes a problem.

## Consequences

DAGs stay thin and only call the pipeline API (see ADR-0002). Airflow's own Postgres is separate from the PostGIS application database.
