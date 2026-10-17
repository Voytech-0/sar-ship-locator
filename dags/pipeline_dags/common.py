"""Settings shared by the DAGs."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from airflow.sdk import Asset

START_DATE = datetime(2026, 10, 1, tzinfo=UTC)

# ais_load updates this Asset; the validation DAG is scheduled on it (ADR-0003).
AIS_LOADED = Asset(name="ais_loaded", uri="sar://ais_loaded")


def default_args() -> dict[str, Any]:
    """Three retries with exponential backoff (docs/PLAN.md). The delay is overridable so tests run fast."""
    return {
        "retries": 3,
        "retry_delay": timedelta(seconds=float(os.environ.get("PIPELINE_RETRY_DELAY_SECONDS", "120"))),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=30),
    }


def ais_lag_days() -> int:
    """DMA publishes a UTC day about 3 days late (ADR-0003); the run for interval start D loads day D-lag."""
    return int(os.environ.get("AIS_LAG_DAYS", "3"))


def ais_extra_days() -> int:
    """Older days each run re-requests, so a day that was late last time gets picked up."""
    return int(os.environ.get("AIS_RETRY_DAYS", "2"))


# A manually triggered run has no data interval in Airflow 3 (`data_interval_start` is None) and may have
# no logical date either, only `run_after` (when it was triggered). Use the first one that exists.
SINCE_TEMPLATE = (
    "{{ ((data_interval_start or logical_date or dag_run.run_after)"
    " - macros.timedelta(days=2)).isoformat() }}"
)


def reference_time(
    data_interval_start: datetime | None, logical_date: datetime | None, run_after: datetime | None = None
) -> datetime:
    """The time a run is about: its data interval start, else its logical date, else when it was triggered."""
    ref = data_interval_start or logical_date or run_after
    if ref is None:
        raise ValueError("run has no data interval, logical date or run_after")
    return ref
