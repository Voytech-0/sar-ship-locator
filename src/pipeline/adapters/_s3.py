"""Shared S3 helpers: error classification, verified ranged GETs, and a seekable range-backed file object."""

from __future__ import annotations

import io
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from urllib3.exceptions import HTTPError as Urllib3Error

from contracts.protocols import PermanentError

from ._retry import RetryPolicy

_RETRYABLE_CODES = {
    "SlowDown",
    "RequestTimeout",
    "InternalError",
    "ServiceUnavailable",
    "RequestTimeTooSkewed",
}


class TruncatedRead(Exception):
    """The server returned fewer bytes than the range asked for."""


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, TruncatedRead | BotoCoreError | Urllib3Error | ConnectionError | TimeoutError):
        return True
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        code = exc.response.get("Error", {}).get("Code", "")
        return status == 429 or status >= 500 or code in _RETRYABLE_CODES
    return False


def raise_if_permanent(exc: ClientError) -> None:
    """Auth, bad request and missing-object errors will not fix themselves."""
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    if status in (400, 401, 403, 404) and not is_retryable(exc):
        raise PermanentError(f"S3 request refused (HTTP {status}): {exc}") from exc


def ranged_get(client: Any, bucket: str, key: str, start: int, end: int, retry: RetryPolicy) -> bytes:
    """Bytes [start, end] inclusive, retried on transient errors and on short reads."""
    expected = end - start + 1

    def once() -> bytes:
        try:
            body = client.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end}")["Body"].read()
        except ClientError as exc:
            raise_if_permanent(exc)
            raise
        if len(body) != expected:
            raise TruncatedRead(f"{key} bytes {start}-{end}: got {len(body)} of {expected}")
        return bytes(body)

    return retry.call(once, is_retryable, f"S3 GET {key} [{start}-{end}]")


class S3RangeReader(io.RawIOBase):
    """Read-only seekable view of an S3 object that fetches aligned blocks on demand (for parsing headers)."""

    def __init__(
        self,
        client: Any,
        bucket: str,
        key: str,
        retry: RetryPolicy,
        block_size: int = 1 << 20,
        size: int | None = None,
    ) -> None:
        self._client, self._bucket, self._key, self._retry = client, bucket, key, retry
        self._block = block_size
        self._pos = 0
        self._cache: dict[int, bytes] = {}
        self.requests = 0
        self.bytes_read = 0
        self.size = size if size is not None else self._head_size()

    def _head_size(self) -> int:
        def once() -> int:
            try:
                return int(self._client.head_object(Bucket=self._bucket, Key=self._key)["ContentLength"])
            except ClientError as exc:
                raise_if_permanent(exc)
                raise

        return self._retry.call(once, is_retryable, f"S3 HEAD {self._key}")

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def _block_bytes(self, index: int) -> bytes:
        if index not in self._cache:
            start = index * self._block
            end = min(start + self._block, self.size) - 1
            self._cache[index] = ranged_get(self._client, self._bucket, self._key, start, end, self._retry)
            self.requests += 1
            self.bytes_read += end - start + 1
        return self._cache[index]

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        want = min(len(view), self.size - self._pos)
        done = 0
        while done < want:
            index, offset = divmod(self._pos, self._block)
            chunk = self._block_bytes(index)[offset : offset + want - done]
            view[done : done + len(chunk)] = chunk
            done += len(chunk)
            self._pos += len(chunk)
        return done
