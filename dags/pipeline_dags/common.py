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
