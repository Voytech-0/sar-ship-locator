"""ObjectStore backed by MinIO (any S3-compatible endpoint)."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from ._retry import RetryPolicy
from ._s3 import is_retryable, raise_if_permanent

_MISSING = {"NoSuchKey", "404", "NotFound"}


def create_minio_client(endpoint_url: str, access_key: str, secret_key: str) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
    )


class MinioObjectStore:
    """Keys are relative to one bucket (default `sar`, see contracts/storage.md).

    `get` raises `FileNotFoundError` for a missing key; `delete` of a missing key is a no-op.
    """

    def __init__(self, client: Any, bucket: str = "sar", retry: RetryPolicy | None = None) -> None:
        self._client = client
        self._bucket = bucket
        self._retry = retry or RetryPolicy(attempts=3, base_delay=0.5)

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in _MISSING:
                raise
            self._client.create_bucket(Bucket=self._bucket)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._call(
            lambda: self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            ),
            f"PUT {key}",
        )

    def get(self, key: str) -> bytes:
        def once() -> bytes:
            body: bytes = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
            return body

        try:
            data: bytes = self._call(once, f"GET {key}")
            return data
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING:
                raise FileNotFoundError(key) from exc
            raise

    def exists(self, key: str) -> bool:
        try:
            self._call(lambda: self._client.head_object(Bucket=self._bucket, Key=key), f"HEAD {key}")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING:
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        self._call(lambda: self._client.delete_object(Bucket=self._bucket, Key=key), f"DELETE {key}")

    def _call(self, fn: Any, what: str) -> Any:
        def guarded() -> Any:
            try:
                return fn()
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in _MISSING:
                    raise
                raise_if_permanent(exc)
                raise

        return self._retry.call(guarded, is_retryable, f"object store {what}")
