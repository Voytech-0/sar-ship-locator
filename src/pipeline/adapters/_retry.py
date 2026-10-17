"""Bounded retry with exponential backoff, shared by the network adapters."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from contracts.protocols import TransientError

T = TypeVar("T")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    def call(self, fn: Callable[[], T], is_retryable: Callable[[BaseException], bool], what: str) -> T:
        """Run `fn`; retry exceptions `is_retryable` accepts, re-raise the others unchanged.

        After `attempts` failed tries raises `TransientError` chained to the last error.
        """
        last: BaseException | None = None
        for attempt in range(self.attempts):
            try:
                return fn()
            except Exception as exc:
                if not is_retryable(exc):
                    raise
                last = exc
                if attempt + 1 < self.attempts:
                    delay = min(self.max_delay, self.base_delay * 2**attempt)
                    log.warning("%s failed (%s), retry %d in %.1fs", what, exc, attempt + 1, delay)
                    self.sleep(delay)
        raise TransientError(f"{what} failed after {self.attempts} attempts: {last}") from last
