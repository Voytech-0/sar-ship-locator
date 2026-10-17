"""Small, dependency-light helpers shared by the fixture gate tests. Not the production algorithms."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from numpy.typing import NDArray
from scipy import ndimage
from scipy.interpolate import RegularGridInterpolator

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load_meta() -> dict[str, Any]:
    meta: dict[str, Any] = json.loads((FIXTURES / "scene" / "meta.json").read_text())
    return meta


def load_expected() -> dict[str, Any]:
    expected: dict[str, Any] = json.loads((FIXTURES / "scene" / "expected_ships.json").read_text())
    return expected


def scene_dir() -> Path:
    key: str = load_meta()["measurement_key"]
    return FIXTURES / "s3" / "eodata" / Path(key).parent.parent


def tiff_path() -> Path:
    key: str = load_meta()["measurement_key"]
    return FIXTURES / "s3" / "eodata" / key


def ais_csv_path() -> Path:
    return next((FIXTURES / "ais").glob("*.csv"))


def load_dn() -> NDArray[np.uint16]:
    data: NDArray[np.uint16] = tifffile.imread(tiff_path())
    return data


def scene_time(meta: dict[str, Any], row: float) -> datetime:
    """Naive-UTC acquisition time of an image row."""
    start = datetime.fromisoformat(meta["acquired_at"].removesuffix("Z"))
    return start + timedelta(seconds=row * meta["azimuth_time_interval_s"])


class Geolocator:
    """(row, col) -> (lon, lat) by bilinear interpolation of the Scene's GCP lattice."""

    def __init__(self, meta: dict[str, Any]) -> None:
        gcps = meta["gcps"]
        self.lines = sorted({g["line"] for g in gcps})
        self.pixels = sorted({g["pixel"] for g in gcps})
        lon = np.zeros((len(self.lines), len(self.pixels)))
        lat = np.zeros_like(lon)
        for g in gcps:
            i, j = self.lines.index(g["line"]), self.pixels.index(g["pixel"])
            lon[i, j], lat[i, j] = g["longitude"], g["latitude"]
        self._lon = RegularGridInterpolator((self.lines, self.pixels), lon)
        self._lat = RegularGridInterpolator((self.lines, self.pixels), lat)

    def __call__(self, row: float, col: float) -> tuple[float, float]:
        return float(self._lon((row, col))), float(self._lat((row, col)))


@dataclass(frozen=True)
class Blob:
    row: float
    col: float
    pixels: int
    peak: int
    background: float


def find_bright_blobs(
    dn: NDArray[np.uint16], min_pixels: int = 40, max_background: float = 170.0
) -> list[Blob]:
    """Bright pixels against a dark local background (open water), clustered into blobs."""
    amp = ndimage.uniform_filter(dn.astype(np.float32), 3)
    coarse = ndimage.median_filter(dn[::4, ::4].astype(np.float32), 15)
    bg = np.kron(coarse, np.ones((4, 4), dtype=np.float32))[: dn.shape[0], : dn.shape[1]]
    bright = (amp > 4 * bg) & (bg < 400)
    labels, n = ndimage.label(ndimage.binary_dilation(bright, iterations=2))
    blobs: list[Blob] = []
    for i in range(1, n + 1):
        ys, xs = np.nonzero((labels == i) & bright)
        if len(ys) < min_pixels:
            continue
        r, c = float(ys.mean()), float(xs.mean())
        background = float(bg[int(r), int(c)])
        if background < max_background:
            blobs.append(Blob(r, c, len(ys), int(dn[ys, xs].max()), background))
    return sorted(blobs, key=lambda b: -b.pixels)


@dataclass(frozen=True)
class AisRow:
    ts: datetime
    mmsi: int
    lat: float
    lon: float
    sog: float | None
    length_m: float | None


def read_ais(path: Path) -> list[AisRow]:
    rows: list[AisRow] = []
    with path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        col = {name.lstrip("# "): i for i, name in enumerate(header)}
        for r in reader:
            try:
                rows.append(
                    AisRow(
                        ts=datetime.strptime(r[col["Timestamp"]], "%d/%m/%Y %H:%M:%S"),
                        mmsi=int(r[col["MMSI"]]),
                        lat=float(r[col["Latitude"]]),
                        lon=float(r[col["Longitude"]]),
                        sog=float(r[col["SOG"]]) if r[col["SOG"]] else None,
                        length_m=float(r[col["Length"]]) if r[col["Length"]] else None,
                    )
                )
            except ValueError:
                continue  # rows with unusable timestamp, id or position
    return rows


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = np.pi / 180
    a = (
        np.sin((lat2 - lat1) * p / 2) ** 2
        + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2
    )
    return float(12742000 * np.arcsin(np.sqrt(a)))


def tracks_by_mmsi(rows: list[AisRow]) -> dict[int, list[AisRow]]:
    tracks: dict[int, list[AisRow]] = {}
    for r in rows:
        tracks.setdefault(r.mmsi, []).append(r)
    for t in tracks.values():
        t.sort(key=lambda r: r.ts)
    return tracks


def position_at(track: list[AisRow], when: datetime) -> tuple[float, float] | None:
    """Linear interpolation of a track to `when`; None if `when` is outside the track's time span."""
    for a, b in pairwise(track):
        if a.ts <= when <= b.ts:
            span = (b.ts - a.ts).total_seconds()
            w = 0.0 if span == 0 else (when - a.ts).total_seconds() / span
            return a.lat + w * (b.lat - a.lat), a.lon + w * (b.lon - a.lon)
    return None


def nearest_vessel(
    tracks: dict[int, list[AisRow]], lon: float, lat: float, when: datetime
) -> tuple[int, float] | None:
    best: tuple[int, float] | None = None
    for mmsi, track in tracks.items():
        pos = position_at(track, when)
        if pos is None:
            continue
        d = haversine_m(lat, lon, pos[0], pos[1])
        if best is None or d < best[1]:
            best = (mmsi, d)
    return best
