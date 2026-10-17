"""Prometheus metrics (contracts/metrics.md). Handlers call the helpers; nothing else touches the registry.

Label values are checked against fixed enums so a free-text value can never become a label.
"""

from __future__ import annotations

from collections.abc import Mapping

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()

STAGES = ("discover", "fetch", "detect", "ais-load", "validate")
JOB_RESULTS = ("succeeded", "failed")
SCENE_STATUSES = ("discovered", "fetched", "detected", "validated", "failed")
SHORE_CLASSES = ("open_water", "near_shore")
MATCH_LABELS = ("matched", "unmatched", "ambiguous")
SOURCES = ("cdse", "dma")
OUTCOMES = ("ok", "error")

_DURATION_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600)

JOBS_TOTAL = Counter("sar_jobs_total", "Finished Jobs", ["stage", "status"], registry=REGISTRY)
JOB_DURATION_SECONDS = Histogram(
    "sar_job_duration_seconds", "Job run time", ["stage"], buckets=_DURATION_BUCKETS, registry=REGISTRY
)
JOBS_IN_FLIGHT = Gauge("sar_jobs_in_flight", "Jobs queued or running", ["stage"], registry=REGISTRY)
SCENES = Gauge("sar_scenes", "Scenes currently in each status", ["region_id", "status"], registry=REGISTRY)
SCENES_DISCOVERED_TOTAL = Counter(
    "sar_scenes_discovered_total", "New Scenes registered", ["region_id"], registry=REGISTRY
)
DETECTIONS_TOTAL = Counter(
    "sar_detections_total",
    "Detections written",
    ["region_id", "detector_name", "shore"],
    registry=REGISTRY,
)
DETECTIONS_DROPPED_ON_LAND_TOTAL = Counter(
    "sar_detections_dropped_on_land_total",
    "Candidates discarded as on land",
    ["region_id", "detector_name"],
    registry=REGISTRY,
)
VALIDATIONS_TOTAL = Counter(
    "sar_validations_total", "Validation outcomes", ["region_id", "label"], registry=REGISTRY
)
AIS_POSITIONS_LOADED_TOTAL = Counter(
    "sar_ais_positions_loaded_total", "AIS Positions loaded", ["region_id"], registry=REGISTRY
)
AIS_LAG_DAYS = Gauge(
    "sar_ais_lag_days", "Days between today and the newest loaded AIS day", ["region_id"], registry=REGISTRY
)
LAST_SCENE_ACQUIRED_TIMESTAMP_SECONDS = Gauge(
    "sar_last_scene_acquired_timestamp_seconds",
    "Acquisition time of the newest Scene",
    ["region_id"],
    registry=REGISTRY,
)
SOURCE_REQUESTS_TOTAL = Counter(
    "sar_source_requests_total",
    "Calls to external data sources",
    ["source", "outcome"],
    registry=REGISTRY,
)
BYTES_DOWNLOADED_TOTAL = Counter(
    "sar_bytes_downloaded_total", "Bytes fetched from external sources", ["source"], registry=REGISTRY
)


def _check(name: str, value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise ValueError(f"{name}={value!r} not in {allowed}")
    return value


def job_enqueued(stage: str) -> None:
    JOBS_IN_FLIGHT.labels(_check("stage", stage, STAGES)).inc()


def record_job_finished(stage: str, status: str, seconds: float) -> None:
    """A Job left `queued`/`running`. `status` is `succeeded` or `failed`; `seconds` is its run time."""
    _check("stage", stage, STAGES)
    _check("status", status, JOB_RESULTS)
    JOBS_TOTAL.labels(stage, status).inc()
    JOB_DURATION_SECONDS.labels(stage).observe(seconds)
    JOBS_IN_FLIGHT.labels(stage).dec()


def set_jobs_in_flight(stage: str, count: int) -> None:
    """Absolute value, for resync from the jobs table on startup."""
    JOBS_IN_FLIGHT.labels(_check("stage", stage, STAGES)).set(count)


def set_scene_counts(region_id: str, counts: Mapping[str, int]) -> None:
    """Sets every status, so a status that dropped to zero is reported as 0, not left stale."""
    for status in counts:
        _check("status", status, SCENE_STATUSES)
    for status in SCENE_STATUSES:
        SCENES.labels(region_id, status).set(counts.get(status, 0))


def scenes_discovered(region_id: str, count: int) -> None:
    SCENES_DISCOVERED_TOTAL.labels(region_id).inc(count)


def detections_written(region_id: str, detector_name: str, shore: str, count: int = 1) -> None:
    DETECTIONS_TOTAL.labels(region_id, detector_name, _check("shore", shore, SHORE_CLASSES)).inc(count)


def detections_dropped_on_land(region_id: str, detector_name: str, count: int) -> None:
    DETECTIONS_DROPPED_ON_LAND_TOTAL.labels(region_id, detector_name).inc(count)


def validations_recorded(region_id: str, label: str, count: int = 1) -> None:
    VALIDATIONS_TOTAL.labels(region_id, _check("label", label, MATCH_LABELS)).inc(count)


def ais_positions_loaded(region_id: str, count: int) -> None:
    AIS_POSITIONS_LOADED_TOTAL.labels(region_id).inc(count)


def set_ais_lag(region_id: str, days: float) -> None:
    AIS_LAG_DAYS.labels(region_id).set(days)


def set_last_scene_acquired(region_id: str, unix_seconds: float) -> None:
    LAST_SCENE_ACQUIRED_TIMESTAMP_SECONDS.labels(region_id).set(unix_seconds)


def observe_source_request(source: str, outcome: str) -> None:
    SOURCE_REQUESTS_TOTAL.labels(
        _check("source", source, SOURCES), _check("outcome", outcome, OUTCOMES)
    ).inc()


def bytes_downloaded(source: str, count: int) -> None:
    BYTES_DOWNLOADED_TOTAL.labels(_check("source", source, SOURCES)).inc(count)


def render() -> bytes:
    """Prometheus text exposition of the pipeline registry (body of `GET /metrics`)."""
    return generate_latest(REGISTRY)


CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
