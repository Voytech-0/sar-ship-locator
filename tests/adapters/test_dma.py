from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from contracts.protocols import NotAvailableError, PermanentError, Region, TransientError
from pipeline.adapters._retry import RetryPolicy
from pipeline.adapters.dma import DmaAisSource

BASE = "https://s3.eu-central-1.amazonaws.com/aisdata.ais.dk"
REGION = Region("gb", "Great Belt", [(10.7, 55.0), (11.3, 55.0), (11.3, 55.7), (10.7, 55.7), (10.7, 55.0)])
NO_WAIT = RetryPolicy(attempts=3, base_delay=0.0, sleep=lambda _s: None)
TODAY = date(2026, 10, 17)

HEADER = (
    "# Timestamp,Type of mobile,MMSI,Latitude,Longitude,Navigational status,ROT,SOG,COG,Heading,IMO,"
    "Callsign,Name,Ship type,Cargo type,Width,Length,Type of position fixing device,Draught,Destination,"
    "ETA,Data source type,A,B,C,D"
)


_TAIL = "Unknown,Unknown,,Undefined,,,,Undefined,,Unknown,,AIS,,,,"


def row(
    ts: str,
    kind: str,
    mmsi: str,
    lat: str,
    lon: str,
    sog: str = "5.1",
    cog: str = "90.0",
    heading: str = "88",
) -> str:
    return f"{ts},{kind},{mmsi},{lat},{lon},Under way using engine,0.0,{sog},{cog},{heading},{_TAIL}"


CSV = "\n".join(
    [
        HEADER,
        row("14/10/2026 05:32:00", "Class A", "219000001", "55.34", "10.97"),  # in region
        row("14/10/2026 05:32:00", "Class A", "219000001", "55.34", "10.97"),  # exact duplicate
        row("14/10/2026 05:33:00", "Class B", "219000002", "55.10", "11.20", sog="", cog="", heading="511"),
        row("14/10/2026 05:33:00", "Base Station", "2190064", "55.34", "10.97"),  # not a vessel
        row("14/10/2026 05:33:00", "Class A", "219000003", "57.83", "9.91"),  # outside region
        row("14/10/2026 05:33:00", "Class A", "219000004", "91.0", "181.0"),  # unavailable position
        row("14/10/2026 05:33:00", "Class A", "219000005", "abc", "10.9"),  # garbage
        row("32/13/2026 05:33:00", "Class A", "219000006", "55.3", "10.9"),  # garbage timestamp
        row("14/10/2026 05:34:00", "Class A", "12345", "55.3", "10.9"),  # not a ship MMSI
    ]
)


def make_zip(csv_text: str = CSV, name: str = "aisdk-2026-10-14.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, csv_text)
    return buf.getvalue()


def source(handler: Any, tmp_path: Path, **kw: Any) -> DmaAisSource:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return DmaAisSource(client, NO_WAIT, today=lambda: TODAY, tmp_dir=tmp_path, **kw)


def test_refuses_plain_http() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        DmaAisSource(base_url="http://aisdata.ais.dk")


def test_latest_available_day_probes_with_head_from_yesterday(tmp_path: Path) -> None:
    probed: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "HEAD" and str(req.url).startswith(BASE + "/aisdk-")
        probed.append(req.url.path.rsplit("aisdk-", 1)[1])
        published = req.url.path.endswith(("2026-10-14.zip", "2026-10-13.zip"))  # 3-day lag
        return httpx.Response(200, headers={"content-length": "10"}) if published else httpx.Response(404)

    assert source(handler, tmp_path).latest_available_day() == date(2026, 10, 14)
    assert probed == ["2026-10-16.zip", "2026-10-15.zip", "2026-10-14.zip"]


def test_latest_available_day_is_none_when_nothing_is_published(tmp_path: Path) -> None:
    assert source(lambda r: httpx.Response(404), tmp_path, max_lookback_days=3).latest_available_day() is None


def test_head_server_errors_are_retried(tmp_path: Path) -> None:
    codes = iter([503, 500])

    def handler(req: httpx.Request) -> httpx.Response:
        code = next(codes, 200)
        return httpx.Response(code, headers={"content-length": "1"})

    assert source(handler, tmp_path).latest_available_day() == date(2026, 10, 16)


def test_positions_filter_to_region_vessels_and_dedupe(tmp_path: Path) -> None:
    payload = make_zip()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(payload))})
        return httpx.Response(200, content=payload)

    got = list(source(handler, tmp_path).positions(date(2026, 10, 14), REGION))
    assert [p.mmsi for p in got] == [219000001, 219000002]
    first, second = got
    assert first.ts == datetime(2026, 10, 14, 5, 32, tzinfo=UTC)
    assert first.position == (10.97, 55.34)
    assert (first.sog_knots, first.cog_deg, first.heading) == (5.1, 90.0, 88)
    assert (second.sog_knots, second.cog_deg, second.heading) == (
        None,
        None,
        None,
    )  # blank / 511 = unavailable
    assert list(tmp_path.iterdir()) == []  # temp download removed once consumed


def test_interrupted_download_is_resumed_with_a_range_request(tmp_path: Path) -> None:
    payload = make_zip(CSV + "\n" * 0)
    cut = len(payload) // 2
    gets: list[str | None] = []

    class Dies(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield payload[:cut]
            raise httpx.ReadError("connection reset")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(payload))})
        gets.append(req.headers.get("range"))
        if len(gets) == 1:
            return httpx.Response(200, stream=Dies())
        start = int(req.headers["range"].split("=")[1].rstrip("-"))
        return httpx.Response(206, content=payload[start:])

    got = list(source(handler, tmp_path).positions(date(2026, 10, 14), REGION))
    assert gets == [None, f"bytes={cut}-"]
    assert [p.mmsi for p in got] == [219000001, 219000002]


def test_unpublished_day_raises_not_available(tmp_path: Path) -> None:
    with pytest.raises(NotAvailableError):
        source(lambda r: httpx.Response(404), tmp_path).positions(date(2026, 10, 16), REGION)
    assert issubclass(NotAvailableError, TransientError)


def test_persistent_download_failure_is_transient_and_cleans_up(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "100"})
        return httpx.Response(503)

    with pytest.raises(TransientError):
        source(handler, tmp_path).positions(date(2026, 10, 14), REGION)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"not a zip file at all", "not a valid zip"),
        (make_zip(CSV.replace("Latitude", "Lat")), "lacks columns"),
        (make_zip(name="readme.txt"), "expected one CSV"),
    ],
)
def test_corrupt_archives_are_permanent(tmp_path: Path, payload: bytes, match: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(payload))})
        return httpx.Response(200, content=payload)

    with pytest.raises(PermanentError, match=match):
        list(source(handler, tmp_path).positions(date(2026, 10, 14), REGION))
    assert list(tmp_path.iterdir()) == []
