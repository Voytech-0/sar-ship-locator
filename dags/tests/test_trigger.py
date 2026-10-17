"""JobPollTrigger and client behaviour against the stub API."""

from __future__ import annotations

import asyncio

import pytest
from airflow.sdk.exceptions import AirflowFailException

from pipeline_dags.client import PipelineApi, TransientApiError
from pipeline_dags.triggers import JobPollTrigger
from tests.stub_api import StubServer


def first_event(trigger: JobPollTrigger) -> dict[str, object]:
    async def go() -> dict[str, object]:
        async for event in trigger.run():
            return dict(event.payload)
        raise AssertionError("trigger yielded nothing")

    return asyncio.run(go())


def test_trigger_serializes_for_the_triggerer() -> None:
    t = JobPollTrigger("http://x/v1", "abc", poll_seconds=1.5)
    assert t.serialize() == (
        "pipeline_dags.triggers.JobPollTrigger",
        {"base_url": "http://x/v1", "job_id": "abc", "poll_seconds": 1.5, "max_errors": 8},
    )


def test_trigger_waits_until_the_job_is_terminal(api: StubServer) -> None:
    api.stub.add_scene("S")
    with PipelineApi() as client:
        job = client.submit("fetch", {"scene_id": "S"}).job
    event = first_event(JobPollTrigger(api.url, job["id"], poll_seconds=0.01))
    assert event["job"]["status"] == "succeeded"  # type: ignore[index]
    assert api.stub.polls[job["id"]] == 2  # queued -> running -> succeeded


def test_trigger_survives_transient_api_errors(api: StubServer) -> None:
    api.stub.add_scene("S")
    with PipelineApi() as client:
        job = client.submit("fetch", {"scene_id": "S"}).job
    api.stub.fail_next_requests = 3
    event = first_event(JobPollTrigger(api.url, job["id"], poll_seconds=0.01))
    assert event["job"]["status"] == "succeeded"  # type: ignore[index]


def test_trigger_gives_up_after_repeated_errors(api: StubServer) -> None:
    api.stub.fail_next_requests = 100
    event = first_event(JobPollTrigger(api.url, "whatever", poll_seconds=0.01, max_errors=3))
    assert "failed 3x" in str(event["error"])
    assert "fatal" not in event


def test_trigger_treats_unknown_job_as_fatal(api: StubServer) -> None:
    event = first_event(JobPollTrigger(api.url, "no-such-job", poll_seconds=0.01))
    assert event["fatal"] is True


def test_client_maps_status_codes_to_retry_semantics(api: StubServer) -> None:
    with PipelineApi() as client:
        with pytest.raises(AirflowFailException):  # 422: body breaks the contract, retrying cannot help
            client.submit("fetch", {"scene": "typo"})
        with pytest.raises(AirflowFailException):  # 404
            client.submit("fetch", {"scene_id": "unknown"})
        api.stub.fail_next_requests = 1
        with pytest.raises(TransientApiError):  # 503
            client.regions()


def test_client_reports_http_200_as_existing_job(api: StubServer) -> None:
    api.stub.add_scene("S")
    with PipelineApi() as client:
        first = client.submit("fetch", {"scene_id": "S"})
        second = client.submit("fetch", {"scene_id": "S"})
    assert (first.existing, second.existing) == (False, True)
    assert first.job["id"] == second.job["id"]


def test_client_follows_scene_pagination(api: StubServer) -> None:
    for i in range(7):
        api.stub.add_scene(f"S{i}", acquired=f"2026-10-0{i + 1}T00:00:00Z")
    with PipelineApi() as client:
        assert len(client.scenes()) == 7
