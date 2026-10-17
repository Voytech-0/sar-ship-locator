"""Async trigger that polls a pipeline Job until it finishes, so no worker slot is held while waiting."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
from airflow.triggers.base import BaseTrigger, TriggerEvent

from pipeline_dags.client import TERMINAL


class JobPollTrigger(BaseTrigger):
    """Fires `{"job": <Job>}` when the Job is terminal, or `{"error": str}` after repeated transient failures.

    Transient HTTP failures are tolerated (the API may restart while a long Job runs); a 404 is fatal.
    """

    def __init__(self, base_url: str, job_id: str, poll_seconds: float = 15.0, max_errors: int = 8) -> None:
        super().__init__()
        self.base_url = base_url
        self.job_id = job_id
        self.poll_seconds = poll_seconds
        self.max_errors = max_errors

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            "pipeline_dags.triggers.JobPollTrigger",
            {
                "base_url": self.base_url,
                "job_id": self.job_id,
                "poll_seconds": self.poll_seconds,
                "max_errors": self.max_errors,
            },
        )

    async def run(self) -> AsyncIterator[TriggerEvent]:  # type: ignore[override]
        errors = 0
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            while True:
                try:
                    resp = await client.get(f"/jobs/{self.job_id}")
                    if resp.status_code == 404:
                        yield TriggerEvent({"error": f"Job {self.job_id} not found", "fatal": True})
                        return
                    if resp.status_code >= 500:
                        raise httpx.HTTPError(f"HTTP {resp.status_code}")
                    job = resp.json()
                    errors = 0
                    if job["status"] in TERMINAL:
                        yield TriggerEvent({"job": job})
                        return
                except (httpx.HTTPError, ValueError) as e:
                    errors += 1
                    if errors >= self.max_errors:
                        yield TriggerEvent({"error": f"polling Job {self.job_id} failed {errors}x: {e!r}"})
                        return
                await asyncio.sleep(self.poll_seconds)
