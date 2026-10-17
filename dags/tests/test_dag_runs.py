"""End-to-end DAG runs (dag.test(), deferred Jobs resolved in-process) against the stub API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from airflow.dag_processing.dagbag import DagBag
from airflow.sdk import DagRunState, TaskInstanceState

from tests.conftest import DAGS
from tests.stub_api import StubServer

LOGICAL_DATE = datetime(2026, 10, 17, tzinfo=UTC)
A, B = "S1A_SCENE_A", "S1A_SCENE_B"


def run(dag_id: str) -> Any:
    bag = DagBag(dag_folder=str(DAGS), bundle_path=DAGS, bundle_name="dags")
    assert not bag.import_errors, bag.import_errors
    return bag.dags[dag_id].test(logical_date=LOGICAL_DATE)


def states(dagrun: Any) -> dict[str, list[str | None]]:
    out: dict[str, list[str | None]] = {}
    for ti in dagrun.get_task_instances():
        out.setdefault(ti.task_id, []).append(ti.state)
    return out


def test_scene_pipeline_fetches_and_detects_every_new_scene(api: StubServer) -> None:
    api.stub.new_by_region["great-belt"] = [A, B]
    dagrun = run("scene_pipeline")
    assert dagrun.state == DagRunState.SUCCESS
    assert {s["status"] for s in api.stub.scenes.values()} == {"detected"}
    assert api.stub.status_codes("fetch") == [202, 202]
    assert api.stub.status_codes("detect") == [202, 202]


def test_scene_pipeline_with_nothing_new_is_a_clean_noop(api: StubServer) -> None:
    dagrun = run("scene_pipeline")
    assert dagrun.state == DagRunState.SUCCESS
    assert api.stub.status_codes("fetch") == []


def test_rerun_follows_existing_jobs_via_http_200(api: StubServer) -> None:
    api.stub.new_by_region["great-belt"] = [A]
    run("scene_pipeline")
    api.stub.new_by_region["great-belt"] = [A]  # re-discovered, e.g. after clearing the DAG run
    api.stub.scenes[A]["status"] = "discovered"  # simulate a stale Scene row: stage Jobs already succeeded
    dagrun = run("scene_pipeline")
    assert dagrun.state == DagRunState.SUCCESS
    assert api.stub.status_codes("discover") == [202, 200]
    assert api.stub.status_codes("fetch") == [202, 200]
    assert api.stub.attempts_of("fetch", A) == 1  # no second Job was created


def test_retryable_failure_is_retried_by_airflow(api: StubServer) -> None:
    api.stub.new_by_region["great-belt"] = [A]
    api.stub.script("fetch", A, "fail_retryable", "fail_retryable", "succeed")
    dagrun = run("scene_pipeline")
    assert dagrun.state == DagRunState.SUCCESS
    assert api.stub.attempts_of("fetch", A) == 3
    assert api.stub.scenes[A]["status"] == "detected"


def test_retryable_failure_gives_up_after_three_retries(api: StubServer) -> None:
    api.stub.new_by_region["great-belt"] = [A]
    api.stub.script("fetch", A, *["fail_retryable"] * 10)
    dagrun = run("scene_pipeline")
    assert dagrun.state == DagRunState.FAILED
    assert api.stub.attempts_of("fetch", A) == 4  # first try + 3 retries
    assert states(dagrun)["process_scene.detect"] == [TaskInstanceState.UPSTREAM_FAILED]


def test_permanent_failure_is_not_retried_and_does_not_stop_other_scenes(api: StubServer) -> None:
    api.stub.new_by_region["great-belt"] = [A, B]
    api.stub.script("fetch", A, "fail_permanent")
    dagrun = run("scene_pipeline")
    assert dagrun.state == DagRunState.FAILED
    assert api.stub.attempts_of("fetch", A) == 1
    assert api.stub.scenes[A]["status"] == "failed"
    assert api.stub.scenes[B]["status"] == "detected"


def test_api_unavailable_is_retried(api: StubServer) -> None:
    api.stub.new_by_region["great-belt"] = [A]
    api.stub.fail_next_requests = 2  # regions lookup fails twice with 503, then recovers
    dagrun = run("scene_pipeline")
    assert dagrun.state == DagRunState.SUCCESS


def test_wrong_scene_status_fails_without_retry(api: StubServer) -> None:
    api.stub.add_scene("S1A_ALREADY_DONE", status="validated")
    api.stub.new_by_region["great-belt"] = [
        "S1A_ALREADY_DONE"
    ]  # discover reports it as new; fetch gets a 409
    dagrun = run("scene_pipeline")
    assert dagrun.state == DagRunState.FAILED
    assert api.stub.status_codes("fetch") == [409]


def test_ais_load_requests_the_lagged_days_per_region(api: StubServer) -> None:
    api.stub.published_days = {"2026-10-14", "2026-10-13", "2026-10-12"}
    dagrun = run("ais_load")
    assert dagrun.state == DagRunState.SUCCESS
    assert {(r, d) for r, d in api.stub.loaded_days} == {
        ("great-belt", "2026-10-14"),
        ("great-belt", "2026-10-13"),
        ("great-belt", "2026-10-12"),
    }


def test_ais_day_not_published_yet_is_skipped_not_failed(api: StubServer) -> None:
    api.stub.published_days = {"2026-10-13", "2026-10-12"}  # newest target day (14th) is late
    dagrun = run("ais_load")
    assert dagrun.state == DagRunState.SUCCESS
    assert api.stub.attempts_of("ais-load", "great-belt/2026-10-14") == 4  # retried 3 times, then skipped
    assert sorted(d for _, d in api.stub.loaded_days) == ["2026-10-12", "2026-10-13"]
    assert TaskInstanceState.SKIPPED in [s for v in states(dagrun).values() for s in v]


def test_validation_only_validates_scenes_with_ais_loaded(api: StubServer) -> None:
    api.stub.add_scene("READY", status="detected", acquired="2026-10-14T05:32:04Z")
    api.stub.add_scene("WAITING", status="detected", acquired="2026-10-16T05:32:04Z")
    api.stub.add_scene("NOT_DETECTED_YET", status="fetched", acquired="2026-10-14T06:00:00Z")
    api.stub.loaded_days = {("great-belt", "2026-10-14")}
    dagrun = run("validation")
    assert dagrun.state == DagRunState.SUCCESS
    assert {s: v["status"] for s, v in api.stub.scenes.items()} == {
        "READY": "validated",
        "WAITING": "detected",
        "NOT_DETECTED_YET": "fetched",
    }


@pytest.mark.parametrize("dag_id", ["scene_pipeline", "ais_load", "validation"])
def test_dags_survive_an_empty_stub(api: StubServer, dag_id: str) -> None:
    assert run(dag_id).state == DagRunState.SUCCESS
