from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from pipeline.adapters._retry import RetryPolicy


@pytest.fixture
def s3() -> Iterator[Any]:
    """A moto S3 client with an empty `eodata` bucket (moto honours HTTP Range on GET)."""
    with mock_aws():
        client = boto3.client(
            "s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test"
        )
        client.create_bucket(Bucket="eodata")
        yield client


@pytest.fixture
def fast_retry() -> RetryPolicy:
    return RetryPolicy(attempts=3, base_delay=0.0, sleep=lambda _s: None)


class FlakyClient:
    """Proxy around a boto3 client; `before_get(call_no, kwargs)` may raise or return a truncation length."""

    def __init__(
        self, inner: Any, before_get: Callable[[int, dict[str, Any]], int | None] | None = None
    ) -> None:
        self._inner = inner
        self._before_get = before_get
        self.get_calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_object(self, **kwargs: Any) -> Any:
        self.get_calls.append(kwargs)
        truncate = self._before_get(len(self.get_calls), kwargs) if self._before_get else None
        resp = self._inner.get_object(**kwargs)
        if truncate is None:
            return resp
        body: bytes = resp["Body"].read()[:truncate]

        class _Body:
            def read(self) -> bytes:
                return body

        return {**resp, "Body": _Body()}


def client_error(status: int, code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )
