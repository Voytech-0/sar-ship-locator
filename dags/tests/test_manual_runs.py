"""A manually triggered run has no data interval in Airflow 3; the DAGs must still compute their dates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jinja2
import pytest

from pipeline_dags.common import SINCE_TEMPLATE, reference_time

LOGICAL = datetime(2026, 10, 17, 16, 19, tzinfo=UTC)
INTERVAL = datetime(2026, 10, 16, tzinfo=UTC)


def render(run_after: datetime | None = LOGICAL, since: str | None = None, **ctx: datetime | None) -> str:
    macros = SimpleNamespace(timedelta=timedelta)
    dag_run = SimpleNamespace(run_after=run_after)
    params = SimpleNamespace(since=since)
    return str(jinja2.Template(SINCE_TEMPLATE).render(macros=macros, dag_run=dag_run, params=params, **ctx))


def test_since_for_a_scheduled_run_uses_the_data_interval() -> None:
    assert render(data_interval_start=INTERVAL, logical_date=LOGICAL) == "2026-10-14T00:00:00+00:00"


def test_since_for_a_manual_run_falls_back_to_the_logical_date() -> None:
    assert render(data_interval_start=None, logical_date=LOGICAL) == "2026-10-15T16:19:00+00:00"


def test_since_for_a_manual_run_without_a_logical_date_uses_run_after() -> None:
    assert render(data_interval_start=None, logical_date=None) == "2026-10-15T16:19:00+00:00"


def test_since_param_overrides_the_computed_value() -> None:
    assert render(since="2026-10-12T00:00:00Z", data_interval_start=INTERVAL) == "2026-10-12T00:00:00Z"
    assert render(since="", data_interval_start=INTERVAL) == "2026-10-14T00:00:00+00:00"


def test_reference_time_prefers_the_interval_then_the_logical_date_then_run_after() -> None:
    assert reference_time(INTERVAL, LOGICAL) == INTERVAL
    assert reference_time(None, LOGICAL) == LOGICAL
    assert reference_time(None, None, LOGICAL) == LOGICAL
    with pytest.raises(ValueError):
        reference_time(None, None)
