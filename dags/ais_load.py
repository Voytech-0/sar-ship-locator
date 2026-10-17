"""Daily: load the newest published AIS day(s) per Region, then update the `ais_loaded` Asset.

The API has no "latest available day" endpoint, so each run targets a fixed lag behind its interval start
(AIS_LAG_DAYS, default 3, ADR-0003) plus AIS_RETRY_DAYS older days. Already-loaded days answer HTTP 200 from
the idempotent Job store, so the extra days cost almost nothing.

Not published yet (a retryable Job failure) is retried by Airflow, and on the last attempt the task is
SKIPPED, not failed: the DAG run stays green, no Asset event is emitted for that day, and the next run
requests it again.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from airflow.sdk import dag, task

from pipeline_dags.client import PipelineApi
from pipeline_dags.common import (
    AIS_LOADED,
    START_DATE,
    ais_extra_days,
    ais_lag_days,
    default_args,
    reference_time,
)
from pipeline_dags.operators import StageJobOperator


@dag(
    dag_id="ais_load",
    schedule="@daily",
    start_date=START_DATE,
    catchup=False,
    max_active_runs=1,
    default_args=default_args(),
    tags=["sar", "ais"],
)
def ais_load() -> None:
    @task
    def region_days(
        data_interval_start: Any = None, logical_date: Any = None, dag_run: Any = None
    ) -> list[dict[str, str]]:
        run_after = getattr(dag_run, "run_after", None)
        target = reference_time(data_interval_start, logical_date, run_after).date() - timedelta(
            days=ais_lag_days()
        )
        days = [target - timedelta(days=n) for n in range(ais_extra_days() + 1)]
        with PipelineApi() as api:
            regions = [r["region_id"] for r in api.regions()]
        return [{"region_id": r, "date": d.isoformat()} for r in regions for d in days]

    StageJobOperator.partial(
        task_id="load_day",
        stage="ais-load",
        skip_when_retries_exhausted=True,
        outlets=[AIS_LOADED],
    ).expand_kwargs(region_days())


ais_load()
