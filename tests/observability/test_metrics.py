import re
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families

from pipeline import metrics
from tests.observability.contract_docs import metric_specs

PIPELINE_METRICS_SRC = Path(metrics.__file__).read_text()


def _collectors() -> dict[str, object]:
    """Exposed family name (counters carry `_total`) -> collector object."""
    found: dict[str, object] = {}
    for collector in vars(metrics).values():
        name = getattr(collector, "_name", None)
        if name and getattr(collector, "_type", None) in {"counter", "gauge", "histogram"}:
            found[name + ("_total" if collector._type == "counter" else "")] = collector
    return found


def test_contract_table_is_parsed() -> None:
    assert len(metric_specs()) == 13


@pytest.mark.parametrize("spec", metric_specs(), ids=lambda s: s.name)
def test_metric_matches_contract(spec: object) -> None:
    from tests.observability.contract_docs import MetricSpec

    assert isinstance(spec, MetricSpec)
    collector = _collectors().get(spec.name)
    assert collector is not None, f"{spec.name} is not defined in metrics.py"
    assert collector._type == spec.type, (spec.name, collector._type)  # type: ignore[attr-defined]
    assert tuple(collector._labelnames) == spec.labels, spec.name  # type: ignore[attr-defined]


def test_every_metric_defined_exactly_once_and_no_extras() -> None:
    declared = re.findall(r'(?:Counter|Gauge|Histogram)\(\s*"(sar_\w+)"', PIPELINE_METRICS_SRC)
    assert sorted(declared) == sorted({s.name for s in metric_specs()})
    assert len(declared) == len(set(declared))


def test_no_free_text_labels() -> None:
    assert all("scene_id" not in s.labels for s in metric_specs())
    assert all("scene_id" not in c._labelnames for c in _collectors().values())  # type: ignore[attr-defined]


def _sample(name: str, **labels: str) -> float | None:
    return metrics.REGISTRY.get_sample_value(name, labels)


def test_exposition_is_valid_and_lists_every_family() -> None:
    metrics.set_ais_lag("exposition-test", 3)
    text = metrics.render().decode()
    # once a labelled series exists, prometheus_client also emits `<name>_created` timestamp gauges
    families = {
        f.name: f.type for f in text_string_to_metric_families(text) if not f.name.endswith("_created")
    }
    # parser reports counters without `_total`
    expected = {s.name.removesuffix("_total"): s.type for s in metric_specs()}
    assert families == expected


def test_record_job_finished_updates_counter_histogram_and_gauge() -> None:
    metrics.job_enqueued("fetch")
    before = _sample("sar_jobs_in_flight", stage="fetch") or 0
    metrics.record_job_finished("fetch", "succeeded", 42.0)
    assert _sample("sar_jobs_total", stage="fetch", status="succeeded") is not None
    assert _sample("sar_job_duration_seconds_count", stage="fetch") is not None
    assert _sample("sar_job_duration_seconds_bucket", stage="fetch", le="60.0") is not None
    assert _sample("sar_jobs_in_flight", stage="fetch") == before - 1


def test_failed_job_counts_separately() -> None:
    b_ok = _sample("sar_jobs_total", stage="detect", status="succeeded") or 0
    b_fail = _sample("sar_jobs_total", stage="detect", status="failed") or 0
    metrics.job_enqueued("detect")
    metrics.record_job_finished("detect", "failed", 1.0)
    assert _sample("sar_jobs_total", stage="detect", status="failed") == b_fail + 1
    assert (_sample("sar_jobs_total", stage="detect", status="succeeded") or 0) == b_ok


def test_set_scene_counts_zeroes_missing_statuses() -> None:
    metrics.set_scene_counts("r1", {"detected": 4, "failed": 1})
    metrics.set_scene_counts("r1", {"detected": 2})
    assert _sample("sar_scenes", region_id="r1", status="detected") == 2
    assert _sample("sar_scenes", region_id="r1", status="failed") == 0
    assert _sample("sar_scenes", region_id="r1", status="validated") == 0


def test_counter_helpers_increment_the_right_series() -> None:
    metrics.scenes_discovered("r2", 3)
    metrics.detections_written("r2", "ca-cfar", "near_shore", 5)
    metrics.detections_dropped_on_land("r2", "ca-cfar", 7)
    metrics.validations_recorded("r2", "ambiguous", 2)
    metrics.ais_positions_loaded("r2", 1000)
    metrics.observe_source_request("dma", "error")
    metrics.bytes_downloaded("cdse", 123)
    assert _sample("sar_scenes_discovered_total", region_id="r2") == 3
    assert _sample("sar_detections_total", region_id="r2", detector_name="ca-cfar", shore="near_shore") == 5
    assert (
        _sample("sar_detections_total", region_id="r2", detector_name="ca-cfar", shore="open_water") is None
    )
    assert _sample("sar_detections_dropped_on_land_total", region_id="r2", detector_name="ca-cfar") == 7
    assert _sample("sar_validations_total", region_id="r2", label="ambiguous") == 2
    assert _sample("sar_ais_positions_loaded_total", region_id="r2") == 1000
    assert _sample("sar_source_requests_total", source="dma", outcome="error") is not None
    assert _sample("sar_bytes_downloaded_total", source="cdse") is not None


def test_gauge_helpers() -> None:
    metrics.set_ais_lag("r3", 3.0)
    metrics.set_last_scene_acquired("r3", 1_760_000_000)
    assert _sample("sar_ais_lag_days", region_id="r3") == 3.0
    assert _sample("sar_last_scene_acquired_timestamp_seconds", region_id="r3") == 1_760_000_000


@pytest.mark.parametrize(
    "call",
    [
        lambda: metrics.job_enqueued("download"),
        lambda: metrics.record_job_finished("fetch", "running", 1),
        lambda: metrics.set_scene_counts("r", {"queued": 1}),
        lambda: metrics.detections_written("r", "d", "on_land"),
        lambda: metrics.validations_recorded("r", "legal"),
        lambda: metrics.observe_source_request("aisstream", "ok"),
        lambda: metrics.bytes_downloaded("s3", 1),
    ],
)
def test_label_values_outside_the_enums_are_rejected(call: object) -> None:
    with pytest.raises(ValueError):
        call()  # type: ignore[operator]
