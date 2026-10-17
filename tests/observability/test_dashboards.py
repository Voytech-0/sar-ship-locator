import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.observability.contract_docs import metric_specs, schema_tables

GRAFANA = Path(__file__).resolve().parents[2] / "deploy" / "grafana"
DASHBOARD_FILES = sorted((GRAFANA / "dashboards").glob("*.json"))


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location("build_dashboards", GRAFANA / "build_dashboards.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _panels(path: Path) -> list[dict[str, Any]]:
    data: dict[str, Any] = json.loads(path.read_text())
    return list(data["panels"])


def test_dashboards_exist() -> None:
    assert {p.name for p in DASHBOARD_FILES} == {"pipeline-health.json", "detections.json"}


def test_committed_json_matches_generator() -> None:
    for name, body in _generator().build().items():
        assert json.loads((GRAFANA / "dashboards" / name).read_text()) == body, (
            f"re-run build_dashboards.py ({name})"
        )


@pytest.mark.parametrize("path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_dashboard_structure(path: Path) -> None:
    data = json.loads(path.read_text())
    assert data["uid"] and data["title"]
    ids = [p["id"] for p in data["panels"]]
    assert len(ids) == len(set(ids))
    for p in data["panels"]:
        assert p["targets"], p["title"]
        grid = p["gridPos"]
        assert grid["x"] + grid["w"] <= 24


def test_datasource_uids_resolve_to_provisioned_datasources() -> None:
    provisioned = yaml.safe_load((GRAFANA / "provisioning/datasources/datasources.yml").read_text())[
        "datasources"
    ]
    uids = {d["uid"]: d["type"] for d in provisioned}
    for path in DASHBOARD_FILES:
        for p in _panels(path):
            assert uids[p["datasource"]["uid"]] == p["datasource"]["type"], p["title"]
            for t in p["targets"]:
                assert t["datasource"] == p["datasource"]


def _promql_metrics(expr: str) -> set[str]:
    names = set(re.findall(r"\bsar_[a-z0-9_]+", expr))
    return {re.sub(r"_(bucket|sum|count)$", "", n) if n.startswith("sar_job_duration") else n for n in names}


def test_promql_only_uses_contract_metrics() -> None:
    known = {s.name for s in metric_specs()}
    used: set[str] = set()
    for path in DASHBOARD_FILES:
        for p in _panels(path):
            for t in p["targets"]:
                if "expr" in t:
                    names = _promql_metrics(t["expr"])
                    assert names, f"no sar_ metric in {p['title']}"
                    used |= names
    assert used <= known, used - known
    # the dashboard must cover the metrics it is meant to show
    assert {
        "sar_jobs_total",
        "sar_job_duration_seconds",
        "sar_scenes",
        "sar_ais_lag_days",
        "sar_last_scene_acquired_timestamp_seconds",
        "sar_source_requests_total",
        "sar_bytes_downloaded_total",
    } <= used


def test_promql_labels_are_contract_labels() -> None:
    labels_by_metric = {s.name: set(s.labels) for s in metric_specs()}
    allowed = set().union(*labels_by_metric.values()) | {"le"}
    for path in DASHBOARD_FILES:
        for p in _panels(path):
            for t in p["targets"]:
                for group in re.findall(r"by \(([^)]*)\)", t.get("expr", "")):
                    for label in (x.strip() for x in group.split(",")):
                        assert label in allowed, (p["title"], label)


def _sql_references(sql: str) -> list[tuple[str, str]]:
    aliases: dict[str, str] = {}
    for table, alias in re.findall(r"\b(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?", sql, re.I):
        if alias.upper() in {"ON", "WHERE", "LEFT", "JOIN", "GROUP", "ORDER", "LIMIT"}:
            alias = ""
        aliases[alias or table] = table
    refs = []
    for alias, column in re.findall(r"\b([a-z_]\w*)\.([a-z_]\w*)\b", sql):
        if alias in aliases:
            refs.append((aliases[alias], column))
    return refs


def test_sql_panels_reference_real_tables_and_columns() -> None:
    tables = schema_tables()
    checked = 0
    for path in DASHBOARD_FILES:
        for p in _panels(path):
            for t in p["targets"]:
                if "rawSql" not in t:
                    continue
                refs = _sql_references(t["rawSql"])
                assert refs, p["title"]
                for table, column in refs:
                    assert table in tables, (p["title"], table)
                    assert column in tables[table], (p["title"], table, column)
                checked += 1
    assert checked >= 4


def test_sql_panels_only_read() -> None:
    for path in DASHBOARD_FILES:
        for p in _panels(path):
            for t in p["targets"]:
                sql = t.get("rawSql", "")
                assert not re.search(r"\b(insert|update|delete|drop|alter|create|truncate)\b", sql, re.I)


def test_geomap_uses_lat_lon_columns_the_query_returns() -> None:
    [geo] = [p for p in _panels(GRAFANA / "dashboards/detections.json") if p["type"] == "geomap"]
    loc = geo["options"]["layers"][0]["location"]
    sql = geo["targets"][0]["rawSql"]
    assert f"AS {loc['latitude']}" in sql and f"AS {loc['longitude']}" in sql
    assert geo["targets"][0]["format"] == "table"


def test_sql_time_panels_return_time_column_first() -> None:
    for name in ("Detections per Scene", "Matched / unmatched / ambiguous per Scene"):
        [panel] = [p for p in _panels(GRAFANA / "dashboards/detections.json") if p["title"] == name]
        assert re.match(r"SELECT\s+s\.acquired_at AS time\b", panel["targets"][0]["rawSql"])


def test_dashboards_provider_points_at_mounted_dir() -> None:
    provider = yaml.safe_load((GRAFANA / "provisioning/dashboards/dashboards.yml").read_text())["providers"][
        0
    ]
    assert provider["type"] == "file"
    assert provider["options"]["path"] == "/var/lib/grafana/dashboards"
