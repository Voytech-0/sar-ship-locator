"""AisSource backed by the Danish Maritime Authority's daily AIS archives (one zipped CSV per UTC day).

Files appear about 3 days late. The bucket is read through the path-style HTTPS endpoint, never plain HTTP.
The zip is downloaded to a temporary file (a zip needs random access to its central directory), then its
CSV is streamed row by row, so memory stays flat even though a day is about 700 MB.

CSV columns (checked against 2026-10-14): `# Timestamp` (dd/mm/yyyy HH:MM:SS, UTC), `Type of mobile`,
`MMSI`, `Latitude`, `Longitude`, `SOG`, `COG`, `Heading`, plus vessel metadata (name, type, length,
width). The file
contains duplicate rows and non-vessel stations (base stations, aids to navigation), which are dropped.
"""

from __future__ import annotations

import io
import logging
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from contracts.protocols import AisPosition, NotAvailableError, PermanentError, Region
from pipeline.algorithms.ais_dma import iter_dma_positions

from ._retry import RetryPolicy

log = logging.getLogger(__name__)

BASE_URL = "https://s3.eu-central-1.amazonaws.com/aisdata.ais.dk"


class _RetryableHttp(Exception):
    pass


def _today_utc() -> date:
    return datetime.now(UTC).date()


class DmaAisSource:
    def __init__(
        self,
        client: httpx.Client | None = None,
        retry: RetryPolicy | None = None,
        base_url: str = BASE_URL,
        today: Callable[[], date] = _today_utc,
        max_lookback_days: int = 14,
        tmp_dir: Path | None = None,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("DMA AIS must be fetched over HTTPS")
        self._client = client or httpx.Client(timeout=httpx.Timeout(60.0, read=120.0), follow_redirects=True)
        self._retry = retry or RetryPolicy()
        self._base = base_url.rstrip("/")
        self._today = today
        self._lookback = max_lookback_days
        self._tmp_dir = tmp_dir

    def _url(self, day: date) -> str:
        return f"{self._base}/aisdk-{day:%Y-%m-%d}.zip"

    # ------------------------------------------------------------------ availability

    def latest_available_day(self) -> date | None:
        """Newest day whose archive is published, probed with HEAD from yesterday backwards."""
        today = self._today()
        for back in range(1, self._lookback + 1):
            day = today - timedelta(days=back)
            if self._head(day) is not None:
                return day
        return None

    def _head(self, day: date) -> int | None:
        """Size of the day's archive, None if not published."""

        def once() -> int | None:
            try:
                resp = self._client.head(self._url(day))
            except httpx.TransportError as exc:
                raise _RetryableHttp(str(exc)) from exc
            if resp.status_code in (403, 404):
                return None
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                raise _RetryableHttp(f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise PermanentError(f"unexpected HTTP {resp.status_code} for {self._url(day)}")
            return int(resp.headers["content-length"])

        return self._retry.call(once, lambda e: isinstance(e, _RetryableHttp), f"HEAD AIS {day}")

    # ------------------------------------------------------------------ positions

    def positions(self, day: date, region: Region) -> Iterable[AisPosition]:
        """All vessel AIS Positions of one UTC day inside the Region. Downloads eagerly, parses lazily."""
        size = self._head(day)
        if size is None:
            raise NotAvailableError(f"AIS archive for {day} is not published")
        tmp = tempfile.TemporaryDirectory(prefix="dma-ais-", dir=self._tmp_dir)
        try:
            path = Path(tmp.name) / f"aisdk-{day:%Y-%m-%d}.zip"
            self._download(day, path, size)
            return self._rows(path, region, tmp)
        except BaseException:
            tmp.cleanup()
            raise

    def _download(self, day: date, path: Path, size: int) -> None:
        url = self._url(day)

        def once() -> None:
            have = path.stat().st_size if path.exists() else 0
            if have == size:
                return
            # identity: Range offsets must refer to the stored bytes, not a transfer encoding
            headers = {"Accept-Encoding": "identity"}
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                with self._client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code == 404:
                        raise NotAvailableError(f"AIS archive for {day} disappeared")
                    if resp.status_code in (408, 429) or resp.status_code >= 500:
                        raise _RetryableHttp(f"HTTP {resp.status_code}")
                    if resp.status_code not in (200, 206):
                        raise PermanentError(f"unexpected HTTP {resp.status_code} for {url}")
                    mode = "ab" if resp.status_code == 206 else "wb"
                    with path.open(mode) as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
            except httpx.TransportError as exc:
                raise _RetryableHttp(str(exc)) from exc
            if path.stat().st_size != size:
                raise _RetryableHttp(f"short download: {path.stat().st_size} of {size} bytes")

        self._retry.call(once, lambda e: isinstance(e, _RetryableHttp), f"GET AIS {day}")

    @staticmethod
    def _rows(path: Path, region: Region, tmp: tempfile.TemporaryDirectory[str]) -> Iterator[AisPosition]:
        try:
            try:
                zf = zipfile.ZipFile(path)
            except zipfile.BadZipFile as exc:
                raise PermanentError(f"{path.name} is not a valid zip: {exc}") from exc
            with zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if len(names) != 1:
                    raise PermanentError(f"{path.name}: expected one CSV, found {names}")
                with zf.open(names[0]) as raw:
                    try:
                        yield from iter_dma_positions(
                            io.TextIOWrapper(raw, encoding="utf-8", newline=""), region
                        )
                    except ValueError as exc:  # header lacks required columns
                        raise PermanentError(f"{path.name}: {exc}") from exc
        finally:
            tmp.cleanup()
