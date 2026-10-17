"""DAG import and configuration gates (no task execution)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from airflow.dag_processing.dagbag import DagBag

from pipeline_dags.common import AIS_LOADED
from tests.conftest import DAGS


def setting_of(task: Any, name: str) -> Any:
    """Constructor setting of a task: mapped tasks keep them in partial_kwargs, plain ones as attributes."""
    return task.partial_kwargs.get(name) if hasattr(task, "partial_kwargs") else getattr(task, name, None)


def stage_of(task: Any) -> str | None:
    stage: str | None = setting_of(task, "stage")
    return stage


@pytest.fixture(scope="module")
def bag() -> DagBag:
    return DagBag(dag_folder=str(DAGS), bundle_path=DAGS, bundle_name="dags")


def test_dags_import_without_errors(bag: DagBag) -> None:
    assert bag.import_errors == {}
    assert sorted(bag.dags) == ["ais_load", "scene_pipeline", "validation"]


@pytest.mark.parametrize("dag_id", ["scene_pipeline", "ais_load"])
def test_ingest_dags_run_daily_without_catchup(bag: DagBag, dag_id: str) -> None:
    dag = bag.dags[dag_id]
    timetable: Any = dag.timetable
    assert timetable.expression == "0 0 * * *"  # @daily
    assert dag.catchup is False
    assert dag.max_active_runs == 1


def test_validation_is_scheduled_on_the_ais_asset(bag: DagBag) -> None:
    timetable: Any = bag.dags["validation"].timetable
    assert [a.name for a in timetable.asset_condition.objects] == [AIS_LOADED.name]
    assert [o.name for o in bag.dags["ais_load"].task_dict["load_day"].outlets] == [AIS_LOADED.name]


@pytest.mark.parametrize("dag_id", ["scene_pipeline", "ais_load", "validation"])
def test_every_task_retries_three_times_with_exponential_backoff(bag: DagBag, dag_id: str) -> None:
    for task in bag.dags[dag_id].tasks:
        assert task.retries == 3, task.task_id
        assert task.retry_exponential_backoff, task.task_id
        assert task.max_retry_delay == timedelta(minutes=30), task.task_id


def test_stage_tasks_are_deferrable_stage_operators(bag: DagBag) -> None:
    stages = {
        ("scene_pipeline", "discover"): "discover",
        ("scene_pipeline", "process_scene.fetch"): "fetch",
        ("scene_pipeline", "process_scene.detect"): "detect",
        ("ais_load", "load_day"): "ais-load",
        ("validation", "validate"): "validate",
    }
    for (dag_id, task_id), stage in stages.items():
        assert stage_of(bag.dags[dag_id].task_dict[task_id]) == stage


def test_fetch_precedes_detect_within_each_scene(bag: DagBag) -> None:
    assert bag.dags["scene_pipeline"].task_dict["process_scene.fetch"].downstream_task_ids == {
        "process_scene.detect"
    }


def test_only_ais_load_skips_when_retries_run_out(bag: DagBag) -> None:
    flags = {
        (d.dag_id, t.task_id): setting_of(t, "skip_when_retries_exhausted")
        for d in bag.dags.values()
        for t in d.tasks
        if stage_of(t) is not None
    }
    assert {k for k, v in flags.items() if v} == {("ais_load", "load_day")}
