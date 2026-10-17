"""Fixtures for the API tests: a throwaway PostGIS container with `contracts/schema.sql` applied.

Tests that need the database skip cleanly when Docker is unavailable. The image is amd64-only, so on
Apple Silicon it runs under emulation (slower to start, otherwise fine).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pipeline import db
from pipeline.api.app import create_app
from pipeline.api.config import Settings
from pipeline.api.models import Stage
from pipeline.db import Database
from pipeline.stages import Registry, default_registry

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "contracts" / "schema.sql"
POSTGIS_IMAGE = "postgis/postgis:16-3.4"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    if not _docker_available():
        pytest.skip("Docker is not available")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(POSTGIS_IMAGE, driver=None) as pg:
        dsn = pg.get_connection_url()
        deadline = time.monotonic() + 120
        while True:  # the image restarts Postgres once during init; wait for a stable server
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    conn.execute("SELECT 1")
                time.sleep(2)
                with psycopg.connect(dsn, autocommit=True) as conn:
                    conn.execute(SCHEMA_SQL.read_text())
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        yield dsn


@pytest.fixture(autouse=True)
def clean_db(request: pytest.FixtureRequest) -> None:
    """Empty every table except `regions` (re-upserted at app startup) before each DB-backed test."""
    if "pg_dsn" not in request.fixturenames:  # includes fixtures pulled in by `api` / `make_api`
        return
    dsn = request.getfixturevalue("pg_dsn")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE jobs, validations, detections, ais_positions, ais_loaded_days, scenes "
            "RESTART IDENTITY CASCADE"
        )


class Api:
    """Test handle on a running app: HTTP client plus direct database and job-runner access."""

    def __init__(self, app: FastAPI, client: TestClient, dsn: str) -> None:
        self.app = app
        self.client = client
        self.db = Database(dsn)

    def drain(self) -> int:
        """Run every queued Job on this thread (workers are off in tests)."""
        runner = self.app.state.runner
        return int(runner.run_pending())

    def submit(self, stage: str, body: dict[str, Any]) -> Any:
        # ASCII-escaped JSON so even lone surrogates can be sent (httpx's own encoder refuses them).
        return self.client.post(
            f"/v1/stages/{stage}", content=json.dumps(body), headers={"content-type": "application/json"}
        )

    def run(self, stage: str, body: dict[str, Any]) -> dict[str, Any]:
        """Submit, drain, and return the finished Job."""
        resp = self.submit(stage, body)
        assert resp.status_code in (200, 202), resp.text
        self.drain()
        job: dict[str, Any] = self.client.get(f"/v1/jobs/{resp.json()['id']}").json()
        return job

    def discover(self) -> list[str]:
        job = self.run("discover", {"region_id": "great-belt"})
        assert job["status"] == "succeeded", job
        ids: list[str] = job["result"]["new_scene_ids"]
        return ids

    def scene(self, scene_id: str) -> dict[str, Any]:
        scene: dict[str, Any] = self.client.get(f"/v1/scenes/{scene_id}").json()
        return scene

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            cur = conn.execute(query, params)
            return cur.fetchall() if cur.description else []


@pytest.fixture
def make_api(pg_dsn: str) -> Iterator[Callable[..., Api]]:
    """Factory: `make_api(registry=..., start_workers=...)`. Apps are shut down at test end."""
    stack = ExitStack()

    def factory(registry: Registry | None = None, *, start_workers: bool = False, workers: int = 2) -> Api:
        settings = Settings(database_url=pg_dsn, workers=workers, poll_interval_s=0.05)
        app = create_app(settings, registry=registry, start_workers=start_workers)
        client = stack.enter_context(TestClient(app, raise_server_exceptions=False))
        return Api(app, client, pg_dsn)

    with stack:
        yield factory


@pytest.fixture
def api(make_api: Callable[..., Api]) -> Api:
    return make_api()


@pytest.fixture
def registry() -> dict[Stage, Any]:
    """A mutable copy of the stub registry that a test can patch (pass it to `make_api`)."""
    return dict(default_registry())


__all__ = ["Api", "db"]
