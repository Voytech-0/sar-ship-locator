"""deploy/airflow/compose.yaml: structure checks always, `docker compose config` when Docker is available."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "airflow" / "compose.yaml"
ENV = {
    "AIRFLOW_DB_PASSWORD": "x",
    "AIRFLOW_FERNET_KEY": "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyMTI=",
    "AIRFLOW_JWT_SECRET": "j",
    "AIRFLOW_API_SECRET_KEY": "k",
}


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())
    return data


def test_expected_services(compose: dict[str, Any]) -> None:
    assert set(compose["services"]) == {
        "airflow-db",
        "airflow-init",
        "airflow-apiserver",
        "airflow-scheduler",
        "airflow-dag-processor",
        "airflow-triggerer",
    }


def test_local_executor_and_own_postgres(compose: dict[str, Any]) -> None:
    env = compose["x-airflow-common"]["environment"]
    assert env["AIRFLOW__CORE__EXECUTOR"] == "LocalExecutor"
    assert "@airflow-db/airflow" in env["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"]
    assert compose["services"]["airflow-db"]["image"].startswith("postgres:")


def test_dags_are_mounted_and_pipeline_api_url_is_configurable(compose: dict[str, Any]) -> None:
    common = compose["x-airflow-common"]
    assert "../../dags:/opt/airflow/dags" in common["volumes"]
    assert common["environment"]["PIPELINE_API_URL"].startswith("${PIPELINE_API_URL:-")


def test_triggerer_runs_because_the_operators_are_deferrable(compose: dict[str, Any]) -> None:
    assert compose["services"]["airflow-triggerer"]["command"] == ["triggerer"]


def test_secrets_are_required_not_defaulted(compose: dict[str, Any]) -> None:
    env = compose["x-airflow-common"]["environment"]
    for key in ("AIRFLOW__CORE__FERNET_KEY", "AIRFLOW__API_AUTH__JWT_SECRET", "AIRFLOW__API__SECRET_KEY"):
        assert ":?" in env[key], key


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_docker_compose_config_is_valid() -> None:
    env = {**os.environ, **ENV}
    probe = subprocess.run(["docker", "info"], capture_output=True, env=env)
    if probe.returncode != 0:
        pytest.skip("docker daemon not running")
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "-q"], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
