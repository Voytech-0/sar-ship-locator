"""Property-based contract run: schemathesis generates requests from `contracts/openapi.yaml` and checks
every response (documented status, media type, body schema, no 5xx) against a live server with stub stages.

The server runs for real (uvicorn on a free port, background workers on) so Jobs actually execute while the
fuzzer is running, which exercises `GET /jobs/{id}` results as well as submits.
"""

from __future__ import annotations

import threading
import time
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import schemathesis
import uvicorn
from hypothesis import HealthCheck, settings
from psycopg.types.json import Jsonb

from pipeline import db
from pipeline.api.app import create_app
from pipeline.api.config import Settings
from pipeline.db import Database

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml"
SEED_IDS = [
    f"SEED_{status.upper()}" for status in ("discovered", "fetched", "detected", "validated", "failed")
]
SEED_JOB = "00000000-0000-4000-8000-000000000001"


def _pick(value: str, pool: list[str]) -> str | None:
    """Deterministically map about half of the generated strings onto known ids so the fuzzer reaches
    the 200/202/409 paths and not only 404 (hypothesis cannot be asked for 'an existing id')."""
    h = zlib.crc32(value.encode("utf-8", "replace"))
    return pool[h % len(pool)] if h % 2 == 0 else None


@schemathesis.hook
def map_body(context: Any, body: Any) -> Any:
    if isinstance(body, dict):
        if isinstance(body.get("region_id"), str) and _pick(body["region_id"], ["great-belt"]):
            body["region_id"] = "great-belt"
        if isinstance(body.get("scene_id"), str) and (known := _pick(body["scene_id"], SEED_IDS)):
            body["scene_id"] = known
    return body


@schemathesis.hook
def map_path_parameters(context: Any, path_parameters: Any) -> Any:
    if isinstance(path_parameters, dict):
        if isinstance(path_parameters.get("scene_id"), str) and (
            known := _pick(path_parameters["scene_id"], SEED_IDS)
        ):
            path_parameters["scene_id"] = known
        if isinstance(path_parameters.get("job_id"), str) and _pick(path_parameters["job_id"], ["x"]):
            path_parameters["job_id"] = SEED_JOB
    return path_parameters


@pytest.fixture(scope="module")
def live_url(pg_dsn: str) -> Iterator[str]:
    app = create_app(Settings(database_url=pg_dsn, workers=2, poll_interval_s=0.05), start_workers=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True
    thread.join(timeout=15)


@pytest.fixture
def contract_schema(live_url: str, pg_dsn: str) -> Any:
    ring = [[10.8, 55.1], [11.0, 55.1], [11.0, 55.3], [10.8, 55.3], [10.8, 55.1]]
    with Database(pg_dsn).connection() as conn:
        for i, scene_id in enumerate(SEED_IDS):
            db.insert_scene(conn, scene_id, "great-belt", datetime(2026, 4, 1 + i, 6, 0, tzinfo=UTC), ring)
            status = scene_id.removeprefix("SEED_").lower()
            if status not in ("discovered", "failed"):
                db.mark_scene_done(conn, scene_id, status, f"scenes/{scene_id}/vv.tif")
        db.mark_scene_failed(conn, "SEED_FAILED", "fetch", "seeded failure")
        db.replace_detections(
            conn,
            "SEED_VALIDATED",
            "ca-cfar",
            "1",
            [{"lon": 10.9, "lat": 55.2, "confidence": 0.8, "shore": "open_water"}],
        )
        conn.execute(
            "INSERT INTO jobs (id, stage, input, status, finished_at, result) "
            "VALUES (%s, 'ais-load', %s, 'succeeded', now(), %s)",
            (SEED_JOB, Jsonb({"region_id": "seed", "date": "2026-01-01"}), Jsonb({"positions_loaded": 0})),
        )
    schema = schemathesis.openapi.from_path(CONTRACT)
    schema.config.update(base_url=live_url)
    return schema


schema = schemathesis.pytest.from_fixture("contract_schema")


@schema.parametrize()
@settings(max_examples=30, deadline=None, suppress_health_check=list(HealthCheck))
def test_api_conforms_to_openapi(case: Any) -> None:
    if case.operation.label == "GET /scenes":
        # Contract gap (reported): GET /scenes documents only 200, but bad query parameters (limit out of
        # range, unknown status) correctly get a 422 problem+json. Accept exactly that until the contract
        # documents it, and still run every other check.
        response = case.call()
        if response.status_code == 422:
            assert response.headers["content-type"][0].startswith("application/problem+json")
            return
    case.call_and_validate()
