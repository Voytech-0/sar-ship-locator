"""Triggered by the `ais_loaded` Asset: validate detected Scenes whose AIS is loaded (ADR-0003)."""

from __future__ import annotations

from airflow.sdk import dag, task

from pipeline_dags.client import PipelineApi
from pipeline_dags.common import AIS_LOADED, START_DATE, default_args
from pipeline_dags.operators import StageJobOperator


@dag(
    dag_id="validation",
    schedule=[AIS_LOADED],
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args=default_args(),
    tags=["sar", "validation"],
)
def validation() -> None:
    @task
    def ready_scene_ids() -> list[str]:
        """Scenes the API says have AIS for their acquisition time; the rest wait for a later run."""
        with PipelineApi() as api:
            return [s["scene_id"] for s in api.scenes(status="detected", ais_ready="true")]

    StageJobOperator.partial(task_id="validate", stage="validate").expand(scene_id=ready_scene_ids())


validation()
