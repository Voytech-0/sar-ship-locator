"""Test environment: an isolated Airflow home with a fresh SQLite DB, set up before Airflow is imported."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

DAGS = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp(prefix="airflow-home-"))

os.environ.update(
    AIRFLOW_HOME=str(HOME),
    AIRFLOW__CORE__DAGS_FOLDER=str(DAGS),
    AIRFLOW__CORE__LOAD_EXAMPLES="False",
    AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://localhost:9/execution/",
    AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=f"sqlite:///{HOME}/airflow.db",
    PIPELINE_RETRY_DELAY_SECONDS="0",
    PIPELINE_POLL_SECONDS="0.02",
    AIS_LAG_DAYS="3",
    AIS_RETRY_DAYS="2",
    NO_PROXY="127.0.0.1,localhost",
)

from tests.stub_api import StubServer  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def airflow_db() -> None:
    subprocess.run([sys.executable, "-m", "airflow", "db", "migrate"], check=True, capture_output=True)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[StubServer]:
    """A running stub API; PIPELINE_API_URL points at it."""
    with StubServer() as server:
        monkeypatch.setenv("PIPELINE_API_URL", server.url)
        yield server
        assert server.stub.violations == [], "stub produced responses that break contracts/openapi.yaml"
