"""Daily: discover new Scenes per Region, then fetch -> detect each one (docs/PLAN.md section 2)."""

from __future__ import annotations

from typing import Any

from airflow.sdk import dag, task, task_group

from pipeline_dags.client import PipelineApi
from pipeline_dags.common import START_DATE, default_args
from pipeline_dags.operators import StageJobOperator


@dag(
    dag_id="scene_pipeline",
    schedule="@daily",
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args=default_args(),
    tags=["sar", "ingest"],
)
def scene_pipeline() -> None:
    @task
    def region_ids() -> list[str]:
        with PipelineApi() as api:
            return [r["region_id"] for r in api.regions()]

    @task
    def new_scene_ids(discoveries: Any) -> list[str]:
        """Flatten the per-Region DiscoverResults into one list of Scene ids."""
        return [sid for result in discoveries for sid in result["new_scene_ids"]]

    # `since` must change every run: a succeeded Job dedupes forever on (stage, input), so a bare
    # {region_id} would return the first run's result each day. The 2-day overlap tolerates late
    # catalogue publication; Scenes already registered are skipped by the API.
    discover = StageJobOperator.partial(
        task_id="discover",
        stage="discover",
        since="{{ (data_interval_start - macros.timedelta(days=2)).isoformat() }}",
    ).expand(region_id=region_ids())

    # A mapped group per Scene: one Scene failing fetch does not stop the others, and its detect is skipped.
    @task_group
    def process_scene(scene_id: str) -> None:
        fetch = StageJobOperator(task_id="fetch", stage="fetch", scene_id=scene_id)
        detect = StageJobOperator(task_id="detect", stage="detect", scene_id=scene_id)
        fetch >> detect

    process_scene.expand(scene_id=new_scene_ids(discover.output))


scene_pipeline()
