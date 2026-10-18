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
    overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    panel: dict[str, Any] = {
        "id": pid,
        "title": title,
        "type": kind,
        "datasource": PG,
        "gridPos": grid,
        "fieldConfig": {"defaults": defaults or {}, "overrides": overrides or []},
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


# Where the browser reaches pipeline-api, which serves the Chip images. Change it in the dashboard's
# settings when the stack is not on localhost.
API_URL = {"type": "constant", "name": "api_url", "query": "http://localhost:8000", "hide": 2}


def dashboard(
    uid: str, title: str, panels: list[dict[str, Any]], variables: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
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
        "templating": {"list": variables or []},
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


DARK_CANDIDATE_DETAIL = """\
SELECT '$api_url/v1/detections/' || c.detection_id || '/chip' AS chip,
       c.acquired_at, c.confidence, c.length_m, c.lat AS latitude, c.lon AS longitude, c.scene_id
FROM dark_ship_candidates c
WHERE $__timeFilter(c.acquired_at)
ORDER BY c.acquired_at DESC, c.confidence DESC
LIMIT 100"""

CHIP_GALLERY = """\
SELECT '$api_url/v1/detections/' || d.id || '/chip' AS chip,
       COALESCE(v.label::text, 'unvalidated') AS label,
       d.confidence, d.length_m, d.shore::text AS shore, s.acquired_at
FROM detections d
JOIN scenes s ON s.scene_id = d.scene_id
LEFT JOIN validations v ON v.detection_id = d.id
WHERE d.chip_key IS NOT NULL AND $__timeFilter(s.acquired_at)
  AND ('$label' = 'All' OR COALESCE(v.label::text, 'unvalidated') = '$label')
  AND ('$shore' = 'All' OR d.shore::text = '$shore')
ORDER BY d.confidence DESC
LIMIT 60"""


def _choice(name: str, label: str, values: list[str], default: str = "All") -> dict[str, Any]:
    options = [{"text": v, "value": v, "selected": v == default} for v in ["All", *values]]
    return {
        "type": "custom",
        "name": name,
        "label": label,
        "query": ",".join(["All", *values]),
        "options": options,
        "current": next(o for o in options if o["selected"]),
    }


# Show a Chip URL column as the image itself (Grafana table cell type "image").
CHIP_IMAGE = [
    {
        "matcher": {"id": "byName", "options": "chip"},
        "properties": [
            {"id": "custom.cellOptions", "value": {"type": "image", "title": "chip"}},
            {"id": "custom.width", "value": 110},
        ],
    }
]
TALL_ROWS = {"cellHeight": "lg", "showHeader": True}


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
                "Dark-ship candidates: chips and detail",
                "table",
                DARK_CANDIDATE_DETAIL,
                _grid(12, 22, 12, 12),
                fmt="table",
                options=TALL_ROWS,
                overrides=CHIP_IMAGE,
            ),
            sql_panel(
                7,
                "Chip gallery (highest confidence first; filter by label and shore above)",
                "table",
                CHIP_GALLERY,
                _grid(0, 34, 24, 16),
                fmt="table",
                options=TALL_ROWS,
                overrides=CHIP_IMAGE,
            ),
        ],
        variables=[
            API_URL,
            _choice("label", "Label", ["matched", "unmatched", "ambiguous", "unvalidated"]),
            _choice("shore", "Shore", ["open_water", "near_shore"]),
        ],
    )


# ---- Match analysis ---------------------------------------------------------------------------------------

_MATCH_FROM = """\
FROM validations v
JOIN detections d ON d.id = v.detection_id
JOIN scenes s ON s.scene_id = d.scene_id
WHERE $__timeFilter(s.acquired_at)
  AND ('$shore' = 'All' OR d.shore::text = '$shore')
  AND ('$detector' = 'All' OR d.detector_version = '$detector')"""

# The AIS speed of the matched vessel at the time of the scene (nearest position of that MMSI in time).
_MATCH_FROM_SPEED = _MATCH_FROM.replace(
    "WHERE $__timeFilter",
    """LEFT JOIN LATERAL (
    SELECT a.sog_knots FROM ais_positions a WHERE a.mmsi = v.matched_mmsi
    ORDER BY abs(extract(epoch FROM (a.ts - s.acquired_at))) LIMIT 1
) ais ON true
WHERE $__timeFilter""",
)

MATCH_OUTCOMES = (
    "SELECT count(*) FILTER (WHERE v.label = 'matched') AS matched,\n"
    "       count(*) FILTER (WHERE v.label = 'unmatched') AS unmatched,\n"
    "       count(*) FILTER (WHERE v.label = 'ambiguous') AS ambiguous\n" + _MATCH_FROM
)
MATCH_QUALITY = (
    "SELECT 100.0 * count(*) FILTER (WHERE v.label = 'matched') / NULLIF(count(*), 0) AS match_rate_pct,\n"
    "       percentile_cont(0.5) WITHIN GROUP (ORDER BY v.match_distance_m) AS median_distance_m,\n"
    "       percentile_cont(0.9) WITHIN GROUP (ORDER BY v.match_distance_m) AS p90_distance_m\n" + _MATCH_FROM
)
MATCH_DISTANCE_HISTOGRAM = (
    "SELECT b::text || '-' || (b + 50)::text AS distance_m, count(*) AS matches\n"
    "FROM (SELECT LEAST(FLOOR(v.match_distance_m / 50), 9)::int * 50 AS b\n"
    + _MATCH_FROM.replace("FROM validations v", "  FROM validations v")
    + "\n  AND v.label = 'matched') t\nGROUP BY b ORDER BY b"
)


def _by_bucket(bucket: str, name: str) -> str:
    return (
        f"SELECT {bucket} AS {name},\n"
        "       count(*) FILTER (WHERE v.label = 'matched') AS matched,\n"
        "       count(*) FILTER (WHERE v.label = 'unmatched') AS unmatched,\n"
        "       count(*) FILTER (WHERE v.label = 'ambiguous') AS ambiguous\n"
        + _MATCH_FROM
        + "\nGROUP BY 1 ORDER BY 1"
    )


MATCH_BY_LENGTH = _by_bucket("LEAST(FLOOR(COALESCE(d.length_m, 0) / 50), 6)::int * 50", "length_from_m")
MATCH_BY_CONFIDENCE = _by_bucket("LEAST(FLOOR(d.confidence * 10), 9)::int / 10.0", "confidence_from")
MATCH_DISTANCE_BY_SPEED = (
    "SELECT CASE WHEN ais.sog_knots IS NULL THEN 'unknown' WHEN ais.sog_knots < 3 THEN '0-3 kn'\n"
    "            WHEN ais.sog_knots < 8 THEN '3-8 kn' WHEN ais.sog_knots < 13 THEN '8-13 kn'\n"
    "            ELSE '13+ kn' END AS vessel_speed,\n"
    "       percentile_cont(0.5) WITHIN GROUP (ORDER BY v.match_distance_m) AS median_distance_m,\n"
    "       count(*) AS matches\n"
    + _MATCH_FROM_SPEED
    + "\n  AND v.label = 'matched'\nGROUP BY 1 ORDER BY min(COALESCE(ais.sog_knots, -1))"
)
MATCH_PER_SCENE = (
    "SELECT s.acquired_at, left(s.scene_id, 40) AS scene, count(*) AS detections,\n"
    "       count(*) FILTER (WHERE v.label = 'matched') AS matched,\n"
    "       count(*) FILTER (WHERE v.label = 'unmatched') AS unmatched,\n"
    "       count(*) FILTER (WHERE v.label = 'ambiguous') AS ambiguous,\n"
    "       100.0 * count(*) FILTER (WHERE v.label = 'matched') / NULLIF(count(*), 0) AS match_rate_pct,\n"
    "       percentile_cont(0.5) WITHIN GROUP (ORDER BY v.match_distance_m) AS median_distance_m\n"
    + _MATCH_FROM
    + "\nGROUP BY s.scene_id, s.acquired_at ORDER BY s.acquired_at DESC"
)
MATCH_MAP = (
    "SELECT ST_Y(d.geom) AS latitude, ST_X(d.geom) AS longitude,\n"
    "       CASE WHEN v.match_distance_m < 100 THEN 0 WHEN v.match_distance_m < 250 THEN 1\n"
    "            ELSE 2 END AS class,\n"
    "       v.match_distance_m, v.matched_mmsi, d.length_m, d.confidence\n"
    + _MATCH_FROM
    + "\n  AND v.label = 'matched'\nLIMIT 2000"
)
_CHIP = "'$api_url/v1/detections/' || d.id || '/chip' AS chip"
MATCH_TABLE = (
    f"SELECT {_CHIP}, v.matched_mmsi AS mmsi, v.match_distance_m, ais.sog_knots, d.confidence, d.length_m,\n"
    "       d.shore::text AS shore, s.acquired_at\n"
    + _MATCH_FROM_SPEED
    + "\n  AND v.label = 'matched'\nORDER BY v.match_distance_m DESC\nLIMIT 40"
)


def _review_table(label: str, order: str) -> str:
    return (
        f"SELECT {_CHIP}, d.confidence, d.length_m, d.shore::text AS shore, s.acquired_at, s.scene_id\n"
        + _MATCH_FROM
        + f"\n  AND v.label = '{label}'\nORDER BY {order}\nLIMIT 40"
    )


MATCH_DISTANCE_COLOURS = [("under 100 m", "green"), ("100-250 m", "yellow"), ("250 m or more", "red")]
MATCH_MAP_DEFAULTS = {
    "color": {"mode": "thresholds"},
    "thresholds": {
        "mode": "absolute",
        "steps": [
            {"color": c, "value": None if i == 0 else i} for i, (_, c) in enumerate(MATCH_DISTANCE_COLOURS)
        ],
    },
    "mappings": [
        {
            "type": "value",
            "options": {
                str(i): {"text": name, "color": colour, "index": i}
                for i, (name, colour) in enumerate(MATCH_DISTANCE_COLOURS)
            },
        }
    ],
}


def _unit_overrides(units: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {"matcher": {"id": "byName", "options": field}, "properties": [{"id": "unit", "value": unit}]}
        for field, unit in units.items()
    ]


_RATE_UNITS = {"match_rate_pct": "percent", "median_distance_m": "lengthm", "p90_distance_m": "lengthm"}
_BARS = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}


def matches() -> dict[str, Any]:
    stacked = {**_BARS, "stacking": "normal"}
    return dashboard(
        "sar-matches",
        "Match analysis",
        [
            sql_panel(1, "Outcomes", "stat", MATCH_OUTCOMES, _grid(0, 0, 10, 4), fmt="table"),
            sql_panel(
                2,
                "Match quality",
                "stat",
                MATCH_QUALITY,
                _grid(10, 0, 14, 4),
                fmt="table",
                defaults={"decimals": 0},
                overrides=_unit_overrides(_RATE_UNITS),
            ),
            sql_panel(
                3,
                "How far the AIS vessel is from the Detection (matched, 50 m buckets)",
                "barchart",
                MATCH_DISTANCE_HISTOGRAM,
                _grid(0, 4, 8, 9),
                fmt="table",
                options={**_BARS, "xField": "distance_m"},
            ),
            sql_panel(
                4,
                "Outcome by Detection length (300 = 300 m or longer)",
                "barchart",
                MATCH_BY_LENGTH,
                _grid(8, 4, 8, 9),
                fmt="table",
                options={**stacked, "xField": "length_from_m"},
            ),
            sql_panel(
                5,
                "Outcome by confidence (0.9 = 0.9 to 1.0)",
                "barchart",
                MATCH_BY_CONFIDENCE,
                _grid(16, 4, 8, 9),
                fmt="table",
                options={**stacked, "xField": "confidence_from"},
            ),
            sql_panel(
                6,
                "Median match distance by vessel speed (AIS speed over ground)",
                "barchart",
                MATCH_DISTANCE_BY_SPEED,
                _grid(0, 13, 8, 9),
                fmt="table",
                options={**_BARS, "xField": "vessel_speed"},
                overrides=_unit_overrides({"median_distance_m": "lengthm"}),
            ),
            sql_panel(
                7,
                "Per scene",
                "table",
                MATCH_PER_SCENE,
                _grid(8, 13, 16, 9),
                fmt="table",
                overrides=_unit_overrides(_RATE_UNITS),
            ),
            sql_panel(
                8,
                "Matched Detections by distance to the AIS vessel",
                "geomap",
                MATCH_MAP,
                _grid(0, 22, 10, 13),
                fmt="table",
                options={
                    **GEOMAP_OPTIONS,
                    "layers": [
                        {
                            **GEOMAP_OPTIONS["layers"][0],
                            "name": "Matched",
                            "config": {
                                "style": {"size": {"fixed": 7}, "color": {"field": "class"}, "opacity": 0.9}
                            },
                        }
                    ],
                },
                defaults=MATCH_MAP_DEFAULTS,
            ),
            sql_panel(
                9,
                "Matches to review: furthest from their AIS vessel first",
                "table",
                MATCH_TABLE,
                _grid(10, 22, 14, 13),
                fmt="table",
                options=TALL_ROWS,
                overrides=CHIP_IMAGE + _unit_overrides({"match_distance_m": "lengthm"}),
            ),
            sql_panel(
                10,
                "Ambiguous: two AIS tracks inside the gate (strongest first)",
                "table",
                _review_table("ambiguous", "d.confidence DESC"),
                _grid(0, 35, 12, 12),
                fmt="table",
                options=TALL_ROWS,
                overrides=CHIP_IMAGE,
            ),
            sql_panel(
                11,
                "Unmatched: longest first (ships without AIS, or false alarms?)",
                "table",
                _review_table("unmatched", "d.length_m DESC NULLS LAST"),
                _grid(12, 35, 12, 12),
                fmt="table",
                options=TALL_ROWS,
                overrides=CHIP_IMAGE,
            ),
        ],
        variables=[
            API_URL,
            _choice("shore", "Shore", ["open_water", "near_shore"], default="open_water"),
            {
                "type": "query",
                "name": "detector",
                "label": "Detector version",
                "datasource": PG,
                "query": (
                    "SELECT v FROM (SELECT 'All' AS v"
                    " UNION SELECT DISTINCT detector_version FROM detections) t ORDER BY (v <> 'All'), v"
                ),
                "refresh": 1,
                "includeAll": False,
                "multi": False,
                "current": {"selected": True, "text": "All", "value": "All"},
                "options": [],
            },
        ],
    )


def build() -> dict[str, dict[str, Any]]:
    return {"pipeline-health.json": health(), "detections.json": detections(), "matches.json": matches()}


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, body in build().items():
        (OUT / name).write_text(json.dumps(body, indent=2) + "\n")
