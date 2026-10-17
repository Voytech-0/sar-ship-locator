"""One operator for every pipeline stage: submit a Job, defer while it runs, surface its outcome."""

from __future__ import annotations

import os
from typing import Any

from airflow.sdk import BaseOperator, Context
from airflow.sdk.exceptions import AirflowFailException, AirflowSkipException

from pipeline_dags.client import PipelineApi, TransientApiError, api_url
from pipeline_dags.triggers import JobPollTrigger


class StageJobOperator(BaseOperator):
    """Runs `POST /stages/{stage}` and waits for the Job (deferrable, ADR-0002).

    - HTTP 200 (a queued/running/succeeded Job for the same input exists) and 202 (new Job) are both
      followed; an already-succeeded Job completes immediately, which makes re-runs idempotent.
    - `error_retryable == false` fails the task with AirflowFailException (no retries); any other failure
      raises a normal exception so Airflow's retry policy applies.
    - `skip_when_retries_exhausted`: on the last attempt a retryable failure skips the task instead of
      failing it. For data that may simply not be published yet (AIS archive lag, ADR-0003).

    Result is the Job's `result` object, pushed to XCom.
    """

    template_fields = ("region_id", "scene_id", "date", "since")

    def __init__(
        self,
        *,
        stage: str,
        region_id: str | None = None,
        scene_id: str | None = None,
        date: str | None = None,
        since: str | None = None,
        skip_when_retries_exhausted: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.stage = stage
        self.region_id = region_id
        self.scene_id = scene_id
        self.date = date
        self.since = since
        self.skip_when_retries_exhausted = skip_when_retries_exhausted

    def body(self) -> dict[str, str]:
        fields = {
            "region_id": self.region_id,
            "scene_id": self.scene_id,
            "date": self.date,
            "since": self.since,
        }
        return {k: v for k, v in fields.items() if v is not None}

    def execute(self, context: Context) -> Any:
        try:
            with PipelineApi() as api:
                submitted = api.submit(self.stage, self.body())
        except TransientApiError as e:
            raise RuntimeError(str(e)) from e
        job = submitted.job
        self.log.info(
            "Job %s (%s) is %s%s",
            job["id"],
            self.stage,
            job["status"],
            " [existing]" if submitted.existing else "",
        )
        if job["status"] in ("succeeded", "failed"):
            return self._finish(context, job)
        self.defer(
            trigger=JobPollTrigger(
                base_url=api_url(),
                job_id=job["id"],
                poll_seconds=float(os.environ.get("PIPELINE_POLL_SECONDS", "15")),
            ),
            method_name="execute_complete",
        )

    def execute_complete(self, context: Context, event: dict[str, Any]) -> Any:
        if "error" in event:
            if event.get("fatal"):
                raise AirflowFailException(event["error"])
            return self._retryable_failure(context, event["error"])
        return self._finish(context, event["job"])

    def _finish(self, context: Context, job: dict[str, Any]) -> Any:
        if job["status"] == "succeeded":
            return job.get("result")
        message = f"{self.stage} Job {job['id']} failed: {job.get('error')}"
        if job.get("error_retryable") is False:
            raise AirflowFailException(message)
        return self._retryable_failure(context, message)

    def _retryable_failure(self, context: Context, message: str) -> Any:
        ti = context["ti"]
        if self.skip_when_retries_exhausted and ti.try_number > ti.max_tries:
            raise AirflowSkipException(f"{message} (retries exhausted; a later run will retry)")
        raise RuntimeError(message)
