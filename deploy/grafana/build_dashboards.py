"""Generates the provisioned dashboard JSON files. Run: `uv run python deploy/grafana/build_dashboards.py`.

The JSON in `dashboards/` is committed; a test fails if it drifts from this generator.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent / "dashboards"

PROM = {"type": "prometheus", "uid": "prometheus"}
PG = {"type": "grafana-postgresql-datasource", "uid": "postgis"}


def _grid(x: int, y: int, w: int, h: int) -> dict[str, int]:
    return {"x": x, "y": y, "w": w, "h": h}


def prom_panel(
    pid: int,
    title: str,
    kind: str,
    expr: str,
    grid: dict[str, int],
    legend: str = "{{stage}}",
    unit: str = "short",
    description: str = "",
) -> dict[str, Any]:
    return {
        "id": pid,
        "title": title,
        "description": description,
        "type": kind,
        "datasource": PROM,
        "gridPos": grid,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [{"refId": "A", "datasource": PROM, "expr": expr, "legendFormat": legend}],
    }


def sql_panel(
    pid: int,
    title: str,
    kind: str,
    sql: str,
    grid: dict[str, int],
    fmt: str = "time_series",
    options: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    panel: dict[str, Any] = {
        "id": pid,
        "title": title,
        "type": kind,
        "datasource": PG,
        "gridPos": grid,
        "fieldConfig": {"defaults": defaults or {}, "overrides": []},
        "targets": [
            {
                "refId": "A",
                "datasource": PG,
                "editorMode": "code",
                "format": fmt,
                "rawQuery": True,
                "rawSql": sql,
            }
        ],
    }
    if options:
        panel["options"] = options
    return panel


def dashboard(uid: str, title: str, panels: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "tags": ["sar-ship-locator"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "time": {"from": "now-30d", "to": "now"},
        "refresh": "1m",
        "panels": panels,
    }


def health() -> dict[str, Any]:
    return dashboard(
        "sar-pipeline-health",
        "Pipeline health",
        [
            prom_panel(
                1,
                "Jobs per hour by stage and status",
                "timeseries",
                "sum by (stage, status) (increase(sar_jobs_total[1h]))",
                _grid(0, 0, 12, 8),
                legend="{{stage}} {{status}}",
            ),
            prom_panel(
                2,
                "Job duration p95",
                "timeseries",
                "histogram_quantile(0.95, sum by (le, stage) (rate(sar_job_duration_seconds_bucket[1h])))",
                _grid(12, 0, 12, 8),
                unit="s",
            ),
            prom_panel(
                3,
                "Jobs in flight",
                "stat",
                "sum by (stage) (sar_jobs_in_flight)",
                _grid(0, 8, 8, 6),
            ),
            prom_panel(
                4,
                "Scenes by status",
                "bargauge",
                "sum by (status) (sar_scenes)",
                _grid(8, 8, 8, 6),
                legend="{{status}}",
            ),
            prom_panel(
                5,
                "New Scenes discovered per day",
                "timeseries",
                "sum by (region_id) (increase(sar_scenes_discovered_total[1d]))",
                _grid(16, 8, 8, 6),
                legend="{{region_id}}",
            ),
            prom_panel(
                6,
                "AIS lag",
                "stat",
                "max by (region_id) (sar_ais_lag_days)",
                _grid(0, 14, 8, 5),
                legend="{{region_id}}",
                unit="d",
                description="Days since the newest loaded AIS day. About 3 is normal (DMA publishes late).",
            ),
            prom_panel(
                7,
                "Age of newest Scene",
                "stat",
                "time() - max by (region_id) (sar_last_scene_acquired_timestamp_seconds)",
                _grid(8, 14, 8, 5),
                legend="{{region_id}}",
                unit="s",
                description="Alert fires above 7 days.",
            ),
            prom_panel(
                8,
                "AIS Positions loaded per day",
                "timeseries",
                "sum by (region_id) (increase(sar_ais_positions_loaded_total[1d]))",
                _grid(16, 14, 8, 5),
                legend="{{region_id}}",
            ),
            prom_panel(
                9,
                "External source requests per hour",
                "timeseries",
                "sum by (source, outcome) (increase(sar_source_requests_total[1h]))",
                _grid(0, 19, 12, 7),
                legend="{{source}} {{outcome}}",
            ),
            prom_panel(
                10,
                "Bytes downloaded per day",
                "timeseries",
                "sum by (source) (increase(sar_bytes_downloaded_total[1d]))",
                _grid(12, 19, 12, 7),
                legend="{{source}}",
                unit="bytes",
            ),
        ],
    )


DETECTIONS_PER_SCENE = """\
SELECT s.acquired_at AS time, count(d.id) AS detections
FROM scenes s
LEFT JOIN detections d ON d.scene_id = s.scene_id
WHERE $__timeFilter(s.acquired_at)
GROUP BY s.scene_id, s.acquired_at
ORDER BY 1"""

LABELS_OVER_TIME = """\
SELECT s.acquired_at AS time, v.label::text AS metric, count(*) AS value
FROM validations v
JOIN detections d ON d.id = v.detection_id
JOIN scenes s ON s.scene_id = d.scene_id
WHERE $__timeFilter(s.acquired_at)
GROUP BY s.acquired_at, v.label
ORDER BY 1"""

LABEL_SPLIT = """\
SELECT v.label::text AS label, count(*) AS detections
FROM validations v
JOIN detections d ON d.id = v.detection_id
JOIN scenes s ON s.scene_id = d.scene_id
WHERE $__timeFilter(s.acquired_at)
GROUP BY v.label"""

RECENT_DETECTIONS = """\
SELECT ST_Y(d.geom) AS latitude, ST_X(d.geom) AS longitude,
       COALESCE(v.label::text, 'unvalidated') AS label,
       CASE v.label WHEN 'unmatched' THEN 0 WHEN 'matched' THEN 1 WHEN 'ambiguous' THEN 2 ELSE 3 END AS class,
       d.confidence, d.length_m, d.shore::text AS shore, s.acquired_at
FROM detections d
JOIN scenes s ON s.scene_id = d.scene_id
LEFT JOIN validations v ON v.detection_id = d.id
WHERE $__timeFilter(s.acquired_at)
ORDER BY s.acquired_at DESC
LIMIT 5000"""

# Marker colour by validation outcome: the `class` column (0 unmatched, 1 matched, 2 ambiguous, 3 none).
LABEL_COLOURS = [
    ("unmatched", "orange"),
    ("matched", "green"),
    ("ambiguous", "yellow"),
    ("unvalidated", "gray"),
]
LABEL_FIELD_DEFAULTS = {
    "color": {"mode": "thresholds"},
    "thresholds": {
        "mode": "absolute",
        "steps": [{"color": c, "value": None if i == 0 else i} for i, (_, c) in enumerate(LABEL_COLOURS)],
    },
    "mappings": [
        {
            "type": "value",
            "options": {
                str(i): {"text": name, "color": c, "index": i} for i, (name, c) in enumerate(LABEL_COLOURS)
            },
        }
    ],
}

GEOMAP_OPTIONS = {
    "view": {"id": "coords", "lat": 55.35, "lon": 11.0, "zoom": 8},
    # OpenStreetMap needs no key. Grafana's "default" basemap is CARTO, which now serves watermarked
    # "API KEY REQUIRED" tiles without one.
    "basemap": {"type": "osm-standard"},
    "layers": [
        {
            "type": "markers",
            "name": "Detections",
            "config": {
                "style": {
                    "size": {"fixed": 5},
                    "color": {"field": "class", "fixed": "blue"},
                    "opacity": 0.8,
                }
            },
            "location": {"mode": "coords", "latitude": "latitude", "longitude": "longitude"},
            "tooltip": True,
        }
    ],
}


# Unmatched, confident, ship-sized open-water Detections (view in contracts/schema.sql). Candidates only.
DARK_SHIP_CANDIDATES = """\
SELECT c.lat AS latitude, c.lon AS longitude, c.confidence, c.length_m, c.scene_id, c.acquired_at
FROM dark_ship_candidates c
WHERE $__timeFilter(c.acquired_at)
ORDER BY c.acquired_at DESC
LIMIT 500"""

DARK_GEOMAP_OPTIONS = {
    **GEOMAP_OPTIONS,
    "layers": [
        {
            **GEOMAP_OPTIONS["layers"][0],
            "name": "Dark-ship candidates",
            "config": {"style": {"size": {"fixed": 9}, "color": {"fixed": "red"}, "opacity": 0.9}},
        }
    ],
}


def detections() -> dict[str, Any]:
    return dashboard(
        "sar-detections",
        "Detections",
        [
            sql_panel(1, "Detections per Scene", "timeseries", DETECTIONS_PER_SCENE, _grid(0, 0, 12, 8)),
            sql_panel(
                2,
                "Matched / unmatched / ambiguous per Scene",
                "timeseries",
                LABELS_OVER_TIME,
                _grid(12, 0, 12, 8),
            ),
            sql_panel(
                3,
                "Validation split",
                "piechart",
                LABEL_SPLIT,
                _grid(0, 8, 8, 9),
                fmt="table",
            ),
            sql_panel(
                4,
                "Recent Detections",
                "geomap",
                RECENT_DETECTIONS,
                _grid(8, 8, 16, 14),
                fmt="table",
                options=GEOMAP_OPTIONS,
                defaults=LABEL_FIELD_DEFAULTS,
            ),
            sql_panel(
                5,
                "Dark-ship candidates (unmatched, open water, 100 m or longer, confidence 0.9 or more)",
                "geomap",
                DARK_SHIP_CANDIDATES,
                _grid(0, 22, 12, 12),
                fmt="table",
                options=DARK_GEOMAP_OPTIONS,
            ),
            sql_panel(
                6,
                "Dark-ship candidates: detail",
                "table",
                DARK_SHIP_CANDIDATES,
                _grid(12, 22, 12, 12),
                fmt="table",
            ),
        ],
    )


def build() -> dict[str, dict[str, Any]]:
    return {"pipeline-health.json": health(), "detections.json": detections()}


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, body in build().items():
        (OUT / name).write_text(json.dumps(body, indent=2) + "\n")
