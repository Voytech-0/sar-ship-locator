"""Streaming parser for the Danish Maritime Authority's daily AIS archives (`aisdk-YYYY-MM-DD.zip`).

The archive holds one CSV per day. It is read row by row (a day is several GB uncompressed), rows outside the
Region's bounding box are discarded after reading only latitude and longitude, and everything else is parsed
and filtered against the Region polygon. Malformed rows are skipped and counted, never fatal.

The column layout is isolated in `COLUMNS` / `TIMESTAMP_FORMAT` below so it is easy to adjust once the real
archive layout is confirmed (see tests/fixtures/README.md when stream B has committed it).
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import shapely
from shapely.geometry import Polygon
from shapely.prepared import prep

from contracts.protocols import AisPosition, Region

# Field name -> CSV header (case-insensitive, a leading '# ' is ignored).
COLUMNS: dict[str, str] = {
    "timestamp": "Timestamp",
    "mobile_type": "Type of mobile",
    "mmsi": "MMSI",
    "lat": "Latitude",
    "lon": "Longitude",
    "sog": "SOG",
    "cog": "COG",
    "heading": "Heading",
}
OPTIONAL_COLUMNS: dict[str, str] = {
    "name": "Name",
    "ship_type": "Ship type",
    "length": "Length",
    "width": "Width",
}
TIMESTAMP_FORMAT = "%d/%m/%Y %H:%M:%S"  # UTC
VESSEL_TYPES = frozenset(
    {"class a", "class b"}
)  # excludes base stations, aids to navigation, SAR aircraft, ...

# AIS "not available" sentinels.
_SOG_NA = 102.3
_COG_NA = 360.0
_HEADING_NA = 511


@dataclass
class ParseStats:
    rows_read: int = 0
    malformed: int = 0
    non_vessel: int = 0
    outside_region: int = 0
    duplicate: int = 0
    yielded: int = 0


def iter_dma_positions(
    source: str | Path | IO[str] | IO[bytes],
    region: Region,
    stats: ParseStats | None = None,
) -> Iterator[AisPosition]:
    """Yield the AIS Positions inside `region` from a DMA daily archive.

    `source` is a path to a `.zip` (its first `.csv` member is used) or `.csv`, or an open text or binary
    stream of CSV data. Counts go into `stats` if given.
    """
    stats = stats if stats is not None else ParseStats()
    polygon = Polygon(list(region.polygon))
    min_lon, min_lat, max_lon, max_lat = polygon.bounds
    prepared = prep(polygon)

    with _open_text(source) as text:
        reader = csv.reader(text)
        try:
            header = next(reader)
        except StopIteration:
            return
        idx = _column_indices(header)
        i_lat, i_lon, i_ts, i_type = idx["lat"], idx["lon"], idx["timestamp"], idx["mobile_type"]
        i_mmsi = idx["mmsi"]
        width = max(idx[f] for f in COLUMNS) + 1
        seen: set[tuple[int, datetime]] = set()

        for row in reader:
            stats.rows_read += 1
            if len(row) < width:
                stats.malformed += 1
                continue
            try:
                lat, lon = float(row[i_lat]), float(row[i_lon])
            except ValueError:
                stats.malformed += 1
                continue
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                stats.malformed += 1  # DMA uses 91 / 181 for "no position"
                continue
            if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
                stats.outside_region += 1
                continue
            if row[i_type].strip().lower() not in VESSEL_TYPES:
                stats.non_vessel += 1
                continue
            if not prepared.contains(shapely.Point(lon, lat)):
                stats.outside_region += 1
                continue
            try:
                mmsi = int(row[i_mmsi])
                ts = datetime.strptime(row[i_ts].strip(), TIMESTAMP_FORMAT).replace(tzinfo=UTC)
            except ValueError:
                stats.malformed += 1
                continue
            if not 100_000_000 <= mmsi <= 999_999_999:
                stats.malformed += 1
                continue
            if (mmsi, ts) in seen:
                stats.duplicate += 1
                continue
            seen.add((mmsi, ts))
            stats.yielded += 1
            yield AisPosition(
                mmsi=mmsi,
                ts=ts,
                position=(lon, lat),
                sog_knots=_optional(row[idx["sog"]], _SOG_NA),
                cog_deg=_optional(row[idx["cog"]], _COG_NA),
                heading=_heading(row[idx["heading"]]),
                name=_static_text(row, idx, "name"),
                ship_type=_static_text(row, idx, "ship_type"),
                length_m=_static_size(row, idx, "length"),
                width_m=_static_size(row, idx, "width"),
            )


def _column_indices(header: list[str]) -> dict[str, int]:
    names = {h.strip().lstrip("#").strip().lower(): i for i, h in enumerate(header)}
    missing = [col for col in COLUMNS.values() if col.lower() not in names]
    if missing:
        raise ValueError(f"DMA AIS header lacks columns: {missing}")
    found = {field: names[col.lower()] for field, col in COLUMNS.items()}
    found.update({f: names[c.lower()] for f, c in OPTIONAL_COLUMNS.items() if c.lower() in names})
    return found


def _optional(raw: str, not_available: float) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return None if value >= not_available else value


def _static_text(row: list[str], idx: dict[str, int], field: str) -> str | None:
    value = row[idx[field]].strip() if field in idx and idx[field] < len(row) else ""
    return value if value and value.lower() not in ("undefined", "unknown") else None


def _static_size(row: list[str], idx: dict[str, int], field: str) -> float | None:
    value = _optional(row[idx[field]], float("inf")) if field in idx and idx[field] < len(row) else None
    return value if value is not None and value > 0 else None


def _heading(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(float(raw))
    except ValueError:
        return None
    return None if value == _HEADING_NA or not 0 <= value < 360 else value


class _open_text:
    """Context manager yielding a text stream for a path (csv or zip) or an already open stream."""

    def __init__(self, source: str | Path | IO[str] | IO[bytes]) -> None:
        self._source = source
        self._closers: list[IO[bytes] | IO[str] | zipfile.ZipFile] = []

    def __enter__(self) -> IO[str]:
        src = self._source
        if isinstance(src, str | Path):
            path = Path(src)
            if path.suffix.lower() == ".zip":
                zf = zipfile.ZipFile(path)
                self._closers.append(zf)
                member = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
                if member is None:
                    raise ValueError(f"no .csv member in {path}")
                binary: IO[bytes] = zf.open(member)
            else:
                binary = path.open("rb")
            self._closers.append(binary)
            return self._wrap(binary)
        if isinstance(src, io.TextIOBase):
            return src
        return self._wrap(src)  # type: ignore[arg-type]

    def _wrap(self, binary: IO[bytes]) -> IO[str]:
        text = io.TextIOWrapper(binary, encoding="utf-8", errors="replace", newline="")
        self._closers.append(text)
        return text

    def __exit__(self, *exc: object) -> None:
        for c in reversed(self._closers):
            c.close()
