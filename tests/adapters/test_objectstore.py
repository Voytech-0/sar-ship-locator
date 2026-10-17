from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from contracts.protocols import PermanentError, TransientError
from pipeline.adapters._retry import RetryPolicy
from pipeline.adapters.objectstore import S3ObjectStore

from .conftest import client_error

NO_WAIT = RetryPolicy(attempts=3, base_delay=0.0, sleep=lambda _s: None)


@pytest.fixture
def client() -> Iterator[Any]:
    with mock_aws():
        yield boto3.client("s3", region_name="us-east-1", aws_access_key_id="t", aws_secret_access_key="t")


@pytest.fixture
def store(client: Any) -> S3ObjectStore:
    s = S3ObjectStore(client, "sar", NO_WAIT)
    s.ensure_bucket()
    return s


def test_put_get_roundtrip_and_content_type(store: S3ObjectStore, client: Any) -> None:
    store.put("chips/s/d.png", b"\x89PNG", "image/png")
    assert store.get("chips/s/d.png") == b"\x89PNG"
    assert client.head_object(Bucket="sar", Key="chips/s/d.png")["ContentType"] == "image/png"


def test_put_overwrites_deterministically(store: S3ObjectStore) -> None:
    store.put("k", b"one", "text/plain")
    store.put("k", b"two", "text/plain")
    assert store.get("k") == b"two"


def test_exists_and_delete(store: S3ObjectStore) -> None:
    assert not store.exists("k")
    store.put("k", b"x", "text/plain")
    assert store.exists("k")
    store.delete("k")
    assert not store.exists("k")
    store.delete("k")  # deleting a missing key is a no-op


def test_get_missing_key_raises_file_not_found(store: S3ObjectStore) -> None:
    with pytest.raises(FileNotFoundError, match="nope"):
        store.get("nope")


def test_ensure_bucket_is_idempotent(client: Any) -> None:
    s = S3ObjectStore(client, "fresh", NO_WAIT)
    s.ensure_bucket()
    s.ensure_bucket()
    assert "fresh" in [b["Name"] for b in client.list_buckets()["Buckets"]]


class FailingClient:
    def __init__(self, inner: Any, errors: list[Exception]) -> None:
        self._inner, self._errors = inner, errors
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def put_object(self, **kw: Any) -> Any:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._inner.put_object(**kw)


def test_transient_errors_are_retried(client: Any) -> None:
    client.create_bucket(Bucket="sar")
    flaky = FailingClient(client, [client_error(503, "SlowDown"), client_error(500, "InternalError")])
    S3ObjectStore(flaky, "sar", NO_WAIT).put("k", b"v", "text/plain")
    assert flaky.calls == 3 and client.get_object(Bucket="sar", Key="k")["Body"].read() == b"v"


def test_persistent_failure_is_transient_and_auth_failure_is_permanent(client: Any) -> None:
    client.create_bucket(Bucket="sar")
    always = FailingClient(client, [client_error(503, "SlowDown")] * 5)
    with pytest.raises(TransientError):
        S3ObjectStore(always, "sar", NO_WAIT).put("k", b"v", "text/plain")
    denied = FailingClient(client, [client_error(403, "AccessDenied")])
    with pytest.raises(PermanentError):
        S3ObjectStore(denied, "sar", NO_WAIT).put("k", b"v", "text/plain")
    assert denied.calls == 1
