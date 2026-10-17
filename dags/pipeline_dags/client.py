"""HTTP client for pipeline-api (contracts/openapi.yaml). Sync; the trigger has its own async poll."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from airflow.sdk.exceptions import AirflowFailException

DEFAULT_URL = "http://pipeline-api:8000/v1"
TERMINAL = {"succeeded", "failed"}


def api_url() -> str:
    return os.environ.get("PIPELINE_API_URL", DEFAULT_URL).rstrip("/")


class TransientApiError(Exception):
    """Network failure or 5xx: Airflow should retry the task."""


def raise_for_response(resp: httpx.Response) -> None:
    """4xx: the request itself is wrong (unknown id, wrong Scene status, bad body), retrying cannot help."""
    if resp.status_code >= 500:
        raise TransientApiError(f"{resp.request.method} {resp.request.url}: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise AirflowFailException(
            f"{resp.request.method} {resp.request.url}: HTTP {resp.status_code} {resp.text[:300]}"
        )


@dataclass(frozen=True)
class Submitted:
    job: dict[str, Any]
    existing: bool  # True for HTTP 200: a queued/running/succeeded Job for the same stage and input


class PipelineApi:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0) -> None:
        self._client = httpx.Client(base_url=base_url or api_url(), timeout=timeout)

    def __enter__(self) -> PipelineApi:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.TransportError as e:
            raise TransientApiError(f"{method} {path}: {e!r}") from e
        raise_for_response(resp)
        return resp

    def submit(self, stage: str, body: dict[str, Any]) -> Submitted:
        resp = self._request("POST", f"/stages/{stage}", json=body)
        return Submitted(job=resp.json(), existing=resp.status_code == 200)

    def regions(self) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = self._request("GET", "/regions").json()
        return data

    def scenes(self, **params: Any) -> list[dict[str, Any]]:
        """All Scenes matching the filters, following pagination."""
        items: list[dict[str, Any]] = []
        while True:
            page = self._request(
                "GET", "/scenes", params={**params, "limit": 500, "offset": len(items)}
            ).json()
            items.extend(page["items"])
            if not page["items"] or len(items) >= page["total"]:
                return items
