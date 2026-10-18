"""Behaviour of the pipeline API with stub stage handlers, checked against `contracts/openapi.yaml`."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest

from contracts.protocols import NotAvailableError, PermanentError, TransientError
from pipeline import db
from pipeline.api.models import DetectRequest, Stage
from pipeline.stages import StageContext
from pipeline.stages.stubs import stub_scene_id
from tests.api.conftest import Api
from tests.api.contract import assert_conforms

SCENE_1 = stub_scene_id("great-belt", 1)
SCENE_2 = stub_scene_id("great-belt", 2)  # acquired 2026-01-02T05:30Z; scene 1 on 2026-01-01T05:30Z


def scene_body(scene_id: str = SCENE_1) -> dict[str, str]:
    return {"scene_id": scene_id}


def fail_with(exc: Exception) -> Callable[..., Any]:
    def handler(ctx: StageContext, request: Any) -> Any:
        raise exc

    return handler


# --- full flow and contract conformance -----------------------------------------------------------


def test_full_pipeline_with_stubs(api: Api) -> None:
    assert api.discover() == [SCENE_1, SCENE_2]
    for scene_id in (SCENE_1, SCENE_2):
        assert api.scene(scene_id)["status"] == "discovered"

    fetch = api.run("fetch", scene_body())
    assert fetch["status"] == "succeeded"
    scene = api.scene(SCENE_1)
    assert scene["status"] == "fetched"
    assert scene["raster_key"] == f"scenes/{SCENE_1}/vv.tif"
    assert scene["detection_count"] is None

    detect = api.run("detect", scene_body())
    assert detect["result"]["detection_count"] == 2
    scene = api.scene(SCENE_1)
    assert (scene["status"], scene["detection_count"]) == ("detected", 2)

    load = api.run("ais-load", {"region_id": "great-belt", "date": "2026-01-01"})
    assert load["result"] == {"positions_loaded": 3}

    validate = api.run("validate", scene_body())
    assert validate["result"] == {"matched": 1, "unmatched": 1, "ambiguous": 0}
    assert api.scene(SCENE_1)["status"] == "validated"

    resp = api.client.get(f"/v1/scenes/{SCENE_1}/detections")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")
    assert_conforms(resp, "get", "/scenes/{scene_id}/detections")
    labels = sorted(f["properties"]["label"] for f in resp.json()["features"])
    assert labels == ["matched", "unmatched"]
    matched = next(f for f in resp.json()["features"] if f["properties"]["label"] == "matched")
    assert matched["properties"]["matched_mmsi"] and matched["properties"]["match_distance_m"] == 120.5
    assert matched["geometry"]["type"] == "Point"


def test_every_stage_job_conforms_to_the_contract(api: Api) -> None:
    api.discover()
    bodies = {
        "discover": {"region_id": "great-belt"},
        "fetch": scene_body(),
        "detect": scene_body(),
        "ais-load": {"region_id": "great-belt", "date": "2026-01-01"},
        "validate": scene_body(),
    }
    for stage, body in bodies.items():
        resp = api.submit(stage, body)
        assert_conforms(resp, "post", f"/stages/{stage}")
        api.drain()
        job = api.client.get(f"/v1/jobs/{resp.json()['id']}")
        assert_conforms(job, "get", "/jobs/{job_id}")
        assert job.json()["status"] == "succeeded", job.json()
    for path, template in [
        ("/v1/regions", "/regions"),
        ("/v1/scenes", "/scenes"),
        (f"/v1/scenes/{SCENE_1}", "/scenes/{scene_id}"),
        ("/v1/healthz", "/healthz"),
        ("/v1/metrics", "/metrics"),
    ]:
        assert_conforms(api.client.get(path), "get", template)


def test_regions_come_from_config(api: Api) -> None:
    regions = api.client.get("/v1/regions").json()
    assert [r["region_id"] for r in regions] == ["great-belt"]
    ring = regions[0]["polygon"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 5
    assert api.sql("SELECT region_id FROM regions") == [{"region_id": "great-belt"}]


def test_discover_is_idempotent_across_runs(api: Api) -> None:
    api.discover()
    job = api.run("discover", {"region_id": "great-belt", "since": "2026-01-01T00:00:00Z"})
    assert job["result"] == {"new_scene_ids": [], "known_scene_count": 2}


# --- job idempotency and retries ------------------------------------------------------------------


def test_same_stage_and_input_returns_existing_job(api: Api) -> None:
    api.discover()
    first = api.submit("fetch", scene_body())
    again = api.submit("fetch", scene_body())
    assert (first.status_code, again.status_code) == (202, 200)
    assert first.json()["id"] == again.json()["id"]
    assert first.json()["status"] == "queued"

    api.drain()
    done = api.submit("fetch", scene_body())  # succeeded Jobs also dedupe, even though the Scene moved on
    assert done.status_code == 200
    assert done.json()["id"] == first.json()["id"] and done.json()["status"] == "succeeded"


def test_detect_input_equivalence(api: Api) -> None:
    api.discover()
    api.run("fetch", scene_body())
    a = api.submit("detect", {"scene_id": SCENE_1, "detector": {"name": "ca-cfar", "version": None}})
    b = api.submit("detect", {"scene_id": SCENE_1, "detector": {"name": "ca-cfar"}})
    assert a.json()["id"] == b.json()["id"]
    other = api.submit("detect", {"scene_id": SCENE_1, "detector": {"name": "ca-cfar", "version": "2"}})
    assert other.status_code == 202 and other.json()["id"] != a.json()["id"]


def test_concurrent_submits_create_one_job(api: Api) -> None:
    api.discover()
    with ThreadPoolExecutor(8) as pool:
        responses = list(pool.map(lambda _: api.submit("fetch", scene_body()), range(16)))
    assert len({r.json()["id"] for r in responses}) == 1
    assert sorted(r.status_code for r in responses).count(202) == 1
    assert api.sql("SELECT count(*)::int AS n FROM jobs WHERE stage = 'fetch'")[0]["n"] == 1


def test_failed_job_allows_retry_and_success_clears_scene_failure(
    make_api: Callable[..., Api], registry: dict[Stage, Any]
) -> None:
    good_fetch = registry[Stage.FETCH]
    registry[Stage.FETCH] = fail_with(TransientError("CDSE rate limited"))
    api = make_api(registry=registry)
    api.discover()

    failed = api.run("fetch", scene_body())
    assert failed["status"] == "failed"
    assert failed["error_retryable"] is True and "CDSE rate limited" in failed["error"]
    assert failed["result"] is None
    scene = api.scene(SCENE_1)
    assert (scene["status"], scene["failed_stage"]) == ("failed", "fetch")
    assert "CDSE rate limited" in scene["error"]

    registry[Stage.FETCH] = good_fetch
    retry = api.submit("fetch", scene_body())  # failed Jobs do not dedupe: a new Job is created
    assert retry.status_code == 202 and retry.json()["id"] != failed["id"]
    api.drain()
    scene = api.scene(SCENE_1)
    assert (scene["status"], scene["failed_stage"], scene["error"]) == ("fetched", None, None)


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (TransientError("boom"), True),
        (NotAvailableError("AIS day not published"), True),
        (OSError("connection reset"), True),
        (PermanentError("corrupt product"), False),
        (ValueError("a bug"), False),
    ],
)
def test_exceptions_map_to_error_retryable(
    make_api: Callable[..., Api], registry: dict[Stage, Any], exc: Exception, retryable: bool
) -> None:
    registry[Stage.FETCH] = fail_with(exc)
    api = make_api(registry=registry)
    api.discover()
    job = api.run("fetch", scene_body())
    assert job["status"] == "failed" and job["error_retryable"] is retryable
    assert type(exc).__name__ in job["error"]


def test_handler_returning_wrong_result_type_fails_permanently(
    make_api: Callable[..., Api], registry: dict[Stage, Any]
) -> None:
    registry[Stage.FETCH] = lambda ctx, request: registry[Stage.DETECT](ctx, DetectRequest(scene_id=SCENE_1))
    api = make_api(registry=registry)
    api.discover()
    job = api.run("fetch", scene_body())
    assert job["status"] == "failed" and job["error_retryable"] is False
    assert "expected FetchResult" in job["error"]


def test_failed_ais_load_does_not_mark_the_day_loaded(
    make_api: Callable[..., Api], registry: dict[Stage, Any]
) -> None:
    registry[Stage.AIS_LOAD] = fail_with(NotAvailableError("not published yet"))
    api = make_api(registry=registry)
    job = api.run("ais-load", {"region_id": "great-belt", "date": "2026-01-01"})
    assert job["status"] == "failed" and job["error_retryable"] is True
    assert api.sql("SELECT count(*)::int AS n FROM ais_loaded_days")[0]["n"] == 0


# --- scene status machine -------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["detect", "validate"])
def test_stage_on_discovered_scene_is_409(api: Api, stage: str) -> None:
    api.discover()
    resp = api.submit(stage, scene_body())
    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert_conforms(resp, "post", f"/stages/{stage}")
    assert api.sql("SELECT count(*)::int AS n FROM jobs WHERE stage <> 'discover'")[0]["n"] == 0


def test_fetch_on_advanced_scene_with_stored_raster_is_409(api: Api) -> None:
    api.discover()
    api.sql("UPDATE scenes SET status = 'detected', raster_key = 'x' WHERE scene_id = %s", (SCENE_1,))
    assert api.submit("fetch", scene_body()).status_code == 409


def test_processed_scene_can_be_fetched_again_once_its_raster_is_gone(api: Api) -> None:
    api.discover()
    api.sql("UPDATE scenes SET status = 'validated', raster_key = NULL WHERE scene_id = %s", (SCENE_1,))
    assert api.submit("fetch", scene_body()).status_code == 202


def test_processed_scene_is_detected_again_only_while_its_raster_is_stored(api: Api) -> None:
    api.discover()
    api.sql("UPDATE scenes SET status = 'validated', raster_key = NULL WHERE scene_id = %s", (SCENE_1,))
    assert api.submit("detect", scene_body()).status_code == 409
    api.sql("UPDATE scenes SET raster_key = 'x' WHERE scene_id = %s", (SCENE_1,))
    assert api.submit("detect", scene_body()).status_code == 202
    assert api.scene(SCENE_1)["status"] == "validated"


def test_detect_job_is_pinned_to_the_region_detector_version(api: Api) -> None:
    api.discover()
    api.run("fetch", scene_body())
    job = api.run("detect", scene_body())
    assert set(job["input"]["detector"]) == {"name", "version"}


def test_failed_scene_only_accepts_the_stage_that_failed(
    make_api: Callable[..., Api], registry: dict[Stage, Any]
) -> None:
    registry[Stage.FETCH] = fail_with(PermanentError("corrupt"))
    api = make_api(registry=registry)
    api.discover()
    api.run("fetch", scene_body())
    assert api.submit("detect", scene_body()).status_code == 409
    assert api.submit("fetch", scene_body()).status_code == 202  # a retry of the failed stage is allowed


def test_stale_job_fails_without_clobbering_scene_status(api: Api) -> None:
    api.discover()
    resp = api.submit("fetch", scene_body())
    api.sql("UPDATE scenes SET status = 'detected', raster_key = 'x' WHERE scene_id = %s", (SCENE_1,))
    api.drain()
    job = api.client.get(f"/v1/jobs/{resp.json()['id']}").json()
    assert job["status"] == "failed" and job["error_retryable"] is False and "stored raster" in job["error"]
    assert api.scene(SCENE_1)["status"] == "detected"


def test_rerunning_detect_replaces_that_detector_versions_rows(api: Api) -> None:
    api.discover()
    api.run("fetch", scene_body())
    api.run("detect", scene_body())
    api.sql("UPDATE scenes SET status = 'fetched' WHERE scene_id = %s", (SCENE_1,))
    api.run("detect", {"scene_id": SCENE_1, "detector": {"name": "ca-cfar", "version": "1"}})
    api.sql("UPDATE scenes SET status = 'fetched' WHERE scene_id = %s", (SCENE_1,))
    other = api.run("detect", {"scene_id": SCENE_1, "detector": {"name": "ca-cfar", "version": "2"}})
    assert other["result"]["detector_version"] == "2"
    versions = api.client.get(f"/v1/scenes/{SCENE_1}/detections", params={"detector_version": "2"}).json()
    assert len(versions["features"]) == 2
    everything = api.client.get(f"/v1/scenes/{SCENE_1}/detections").json()
    assert len(everything["features"]) == 4


# --- 404 / 422 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "body"),
    [
        ("discover", {"region_id": "nowhere"}),
        ("fetch", {"scene_id": "missing"}),
        ("detect", {"scene_id": "missing"}),
        ("validate", {"scene_id": "missing"}),
        ("ais-load", {"region_id": "nowhere", "date": "2026-01-01"}),
    ],
)
def test_unknown_region_or_scene_is_404(api: Api, stage: str, body: dict[str, str]) -> None:
    resp = api.submit(stage, body)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert_conforms(resp, "post", f"/stages/{stage}")


@pytest.mark.parametrize(
    "path",
    [
        f"/v1/jobs/{uuid.uuid4()}",
        "/v1/jobs/not-a-uuid",
        "/v1/scenes/missing",
        "/v1/scenes/missing/detections",
        "/v1/scenes/a%00b",
        "/v1/no-such-route",
    ],
)
def test_unknown_resources_are_404_problem_json(api: Api, path: str) -> None:
    resp = api.client.get(path)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert {"title", "status"} <= resp.json().keys()


@pytest.mark.parametrize(
    ("stage", "body"),
    [
        ("discover", {}),
        ("discover", {"region_id": "great-belt", "extra": 1}),
        ("discover", {"region_id": "great-belt", "since": "yesterday"}),
        ("discover", {"region_id": 5}),
        ("detect", {"scene_id": SCENE_1, "detector": {"version": "1"}}),
        ("ais-load", {"region_id": "great-belt", "date": "2026-13-45"}),
    ],
)
def test_bad_bodies_are_422_problem_json(api: Api, stage: str, body: dict[str, Any]) -> None:
    resp = api.submit(stage, body)
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert_conforms(resp, "post", f"/stages/{stage}")


def test_non_json_body_is_422(api: Api) -> None:
    resp = api.client.post(
        "/v1/stages/fetch", content=b"not json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 422


# --- scenes listing -------------------------------------------------------------------------------


def seed_scenes(api: Api, n: int) -> list[str]:
    ring = [[10.8, 55.1], [11.0, 55.1], [11.0, 55.3], [10.8, 55.3], [10.8, 55.1]]
    ids = []
    with api.db.connection() as conn:
        for i in range(n):
            scene_id = f"SEED_{i:02d}"
            db.insert_scene(conn, scene_id, "great-belt", datetime(2026, 3, 1 + i, 6, 0, tzinfo=UTC), ring)
            ids.append(scene_id)
    return ids


def test_scene_pagination_newest_first(api: Api) -> None:
    ids = seed_scenes(api, 5)
    page = api.client.get("/v1/scenes", params={"limit": 2}).json()
    assert page["total"] == 5
    assert [s["scene_id"] for s in page["items"]] == [ids[4], ids[3]]
    page = api.client.get("/v1/scenes", params={"limit": 2, "offset": 2}).json()
    assert [s["scene_id"] for s in page["items"]] == [ids[2], ids[1]]
    page = api.client.get("/v1/scenes", params={"limit": 2, "offset": 4}).json()
    assert [s["scene_id"] for s in page["items"]] == [ids[0]] and page["total"] == 5
    assert api.client.get("/v1/scenes", params={"offset": 99}).json() == {"items": [], "total": 5}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}, {"status": "bogus"}])
def test_scene_listing_rejects_bad_parameters(api: Api, params: dict[str, Any]) -> None:
    assert api.client.get("/v1/scenes", params=params).status_code == 422


def test_scene_filters(api: Api) -> None:
    ids = seed_scenes(api, 3)
    api.sql("UPDATE scenes SET status = 'fetched' WHERE scene_id = %s", (ids[0],))
    by_status = api.client.get("/v1/scenes", params={"status": "fetched"}).json()
    assert [s["scene_id"] for s in by_status["items"]] == [ids[0]] and by_status["total"] == 1
    assert api.client.get("/v1/scenes", params={"region_id": "great-belt"}).json()["total"] == 3
    assert api.client.get("/v1/scenes", params={"region_id": "other"}).json()["total"] == 0
    assert api.client.get("/v1/scenes", params={"region_id": "a\x00b"}).json()["total"] == 0


def test_failed_scenes_are_listable_as_dead_letters(
    make_api: Callable[..., Api], registry: dict[Stage, Any]
) -> None:
    registry[Stage.FETCH] = fail_with(PermanentError("corrupt"))
    api = make_api(registry=registry)
    api.discover()
    api.run("fetch", scene_body())
    failed = api.client.get("/v1/scenes", params={"status": "failed"}).json()
    assert [s["scene_id"] for s in failed["items"]] == [SCENE_1]
    assert failed["items"][0]["failed_stage"] == "fetch"


# --- ais_ready ------------------------------------------------------------------------------------


def ready_ids(api: Api, flag: bool) -> set[str]:
    page = api.client.get("/v1/scenes", params={"ais_ready": str(flag).lower(), "limit": 500}).json()
    return {s["scene_id"] for s in page["items"]}


def test_ais_ready_follows_loaded_days(api: Api) -> None:
    api.discover()  # scene 1 on 2026-01-01 05:30Z, scene 2 on 2026-01-02 05:30Z, margin 30 min
    assert ready_ids(api, True) == set()
    assert ready_ids(api, False) == {SCENE_1, SCENE_2}

    api.run("ais-load", {"region_id": "great-belt", "date": "2026-01-01"})
    assert ready_ids(api, True) == {SCENE_1}
    assert ready_ids(api, False) == {SCENE_2}

    api.run("ais-load", {"region_id": "great-belt", "date": "2026-01-02"})
    assert ready_ids(api, True) == {SCENE_1, SCENE_2}
    assert ready_ids(api, False) == set()


def test_ais_ready_needs_both_days_when_margin_crosses_midnight(api: Api) -> None:
    ring = [[10.8, 55.1], [11.0, 55.1], [11.0, 55.3], [10.8, 55.3], [10.8, 55.1]]
    with api.db.connection() as conn:  # 23:50Z with a 30 minute margin touches 02-01 and 02-02
        db.insert_scene(conn, "NIGHT", "great-belt", datetime(2026, 2, 1, 23, 50, tzinfo=UTC), ring)
    api.run("ais-load", {"region_id": "great-belt", "date": "2026-02-01"})
    assert ready_ids(api, True) == set()
    api.run("ais-load", {"region_id": "great-belt", "date": "2026-02-02"})
    assert ready_ids(api, True) == {"NIGHT"}


def test_ais_ready_is_per_region(api: Api) -> None:
    ring = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
    with api.db.connection() as conn:
        db.upsert_region(conn, "elsewhere", "Elsewhere", ring)
        db.insert_scene(conn, "ELSE", "elsewhere", datetime(2026, 2, 1, 6, 0, tzinfo=UTC), ring)
        db.insert_scene(conn, "HERE", "great-belt", datetime(2026, 2, 1, 6, 0, tzinfo=UTC), ring)
    api.run("ais-load", {"region_id": "great-belt", "date": "2026-02-01"})
    assert ready_ids(api, True) == {"HERE"}


def test_ais_ready_combines_with_status_filter(api: Api) -> None:
    api.discover()
    api.run("ais-load", {"region_id": "great-belt", "date": "2026-01-01"})
    params = {"ais_ready": "true", "status": "detected"}
    assert api.client.get("/v1/scenes", params=params).json()["total"] == 0
    api.run("fetch", scene_body())
    api.run("detect", scene_body())
    assert api.client.get("/v1/scenes", params=params).json()["total"] == 1


# --- restart recovery, workers, health ------------------------------------------------------------


def test_jobs_left_running_are_failed_retryable_on_startup(make_api: Callable[..., Api]) -> None:
    first = make_api()
    first.discover()
    resp = first.submit("fetch", scene_body())
    with first.db.connection() as conn:  # a worker claimed the Job, then the process died
        claimed = db.claim_next_job(conn)
    assert claimed is not None and claimed["status"] == "running"

    second = make_api()  # startup recovery
    job = second.client.get(f"/v1/jobs/{resp.json()['id']}").json()
    assert job["status"] == "failed" and job["error_retryable"] is True
    assert "interrupted by service restart" in job["error"]
    scene = second.scene(SCENE_1)
    assert (scene["status"], scene["failed_stage"]) == ("failed", "fetch")

    retry = second.submit("fetch", scene_body())  # Airflow's retry creates a fresh Job
    assert retry.status_code == 202
    second.drain()
    assert second.scene(SCENE_1)["status"] == "fetched"


def test_queued_jobs_survive_a_restart(make_api: Callable[..., Api]) -> None:
    first = make_api()
    first.discover()
    queued = first.submit("fetch", scene_body()).json()
    second = make_api()
    second.drain()
    assert second.client.get(f"/v1/jobs/{queued['id']}").json()["status"] == "succeeded"


def test_background_workers_process_jobs(make_api: Callable[..., Api]) -> None:
    api = make_api(start_workers=True)
    resp = api.submit("discover", {"region_id": "great-belt"})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = api.client.get(f"/v1/jobs/{resp.json()['id']}").json()
        if job["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "succeeded", job
    assert job["started_at"] and job["finished_at"]


def test_healthz_reports_dependency_failure(api: Api) -> None:
    def broken() -> None:
        raise ConnectionError("db down")

    api.app.state.health_checks["database"] = broken
    resp = api.client.get("/v1/healthz")
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert_conforms(resp, "get", "/healthz")
    assert "database" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("stage", "body"),
    [
        ("discover", {"region_id": "a\x00b"}),
        ("fetch", {"scene_id": "a\x00b"}),
        ("detect", {"scene_id": SCENE_1, "detector": {"name": "ca\x00cfar"}}),
        ("validate", {"scene_id": "\ud800"}),
        ("ais-load", {"region_id": "great-belt\x00", "date": "2026-01-01"}),
    ],
)
def test_identifiers_the_database_cannot_hold_are_unknown_not_errors(
    api: Api, stage: str, body: dict[str, Any]
) -> None:
    api.discover()
    resp = api.submit(stage, body)
    assert resp.status_code == 404
    assert_conforms(resp, "post", f"/stages/{stage}")
