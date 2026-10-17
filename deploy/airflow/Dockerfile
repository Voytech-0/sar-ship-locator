# Airflow image with the project DAGs baked in. Build context: dags/ (see compose.yaml).
# Pinned to the Airflow version dags/pyproject.toml is constrained to; httpx ships with the base image.
FROM apache/airflow:3.3.2-python3.12

COPY --chown=airflow:root . /opt/airflow/dags/

# The DAG files import the shared pipeline_dags package, and the triggerer imports JobPollTrigger from it:
# make it importable in every Airflow process, not only where the DAG bundle path is added to sys.path.
ENV PYTHONPATH=/opt/airflow/dags
