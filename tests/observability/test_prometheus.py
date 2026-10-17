import re
import subprocess
from pathlib import Path

import yaml

from tests.observability.conftest import requires_docker
from tests.observability.contract_docs import metric_specs

PROM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "prometheus"
IMAGE = "prom/prometheus:v2.55.1"


def _promtool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "promtool",
            "-v",
            f"{PROM_DIR}:/etc/prometheus:ro",
            IMAGE,
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_scrape_config_targets_pipeline_api() -> None:
    cfg = yaml.safe_load((PROM_DIR / "prometheus.yml").read_text())
    [job] = cfg["scrape_configs"]
    assert job["metrics_path"] == "/metrics"
    assert job["static_configs"][0]["targets"] == ["pipeline-api:8000"]
    assert cfg["rule_files"] == ["/etc/prometheus/alerts.yml"]


def test_alert_rules_use_contract_metrics_and_agreed_threshold() -> None:
    rules = yaml.safe_load((PROM_DIR / "alerts.yml").read_text())["groups"][0]["rules"]
    known = {s.name for s in metric_specs()}
    for rule in rules:
        assert set(re.findall(r"\bsar_[a-z_]+", rule["expr"])) <= known
    stale = next(r for r in rules if r["alert"] == "NoNewSceneIn7Days")
    assert "sar_last_scene_acquired_timestamp_seconds" in stale["expr"]
    assert "7 * 24 * 3600" in stale["expr"]


@requires_docker
def test_promtool_checks_config_and_rules() -> None:
    result = _promtool("check", "config", "/etc/prometheus/prometheus.yml")
    assert result.returncode == 0, result.stdout + result.stderr
    result = _promtool("check", "rules", "/etc/prometheus/alerts.yml")
    assert result.returncode == 0, result.stdout + result.stderr


@requires_docker
def test_promtool_alert_rule_unit_tests() -> None:
    result = _promtool("test", "rules", "/etc/prometheus/alerts_test.yml")
    assert result.returncode == 0, result.stdout + result.stderr
