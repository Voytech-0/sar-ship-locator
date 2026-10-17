# Pipeline stages sit behind an async HTTP job API

Status: accepted

Airflow does not run pipeline code. Each stage (`discover`, `fetch`, `detect`, `validate`, `ais-load`) is an endpoint of a FastAPI service in its own container with the geospatial dependencies (GDAL, rasterio). Airflow submits a job with `POST /jobs`, then polls `GET /jobs/{id}` until it finishes. Jobs are idempotent per Scene id, and large data moves through the object store and Postgres, not through request payloads.

## Considered Options

- **DockerOperator**: needs the Docker socket mounted into Airflow, which is a privilege escalation and couples orchestration to the host.
- **Running code inside the Airflow image**: drags GDAL and rasterio into Airflow's dependency set.
- **Synchronous HTTP calls**: a Scene takes minutes, so calls would hit timeouts.

## Consequences

The pipeline package also works as a plain CLI and in tests without Airflow. Job state needs storage; it lives in Postgres.
