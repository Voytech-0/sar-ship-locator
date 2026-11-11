"""Synthetic Sentinel-1 GRD products: row-striped TIFFs with GCPs, calibration XML, S3 key layout."""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any, Literal

import numpy as np
import tifffile
from numpy.typing import NDArray

SCENE_ID = "S1D_IW_GRDH_1SDV_20261014T053204_20261014T053229_005007_009759_0CC6"
PREFIX = f"Sentinel-1/SAR/IW_GRDH_1S/2026/10/14/{SCENE_ID}.SAFE/"
VV_KEY = PREFIX + "measurement/s1d-iw-grd-vv-20261014t053204-20261014t053229-005007-009759-001.tiff"
VH_KEY = PREFIX + "measurement/s1d-iw-grd-vh-20261014t053204-20261014t053229-005007-009759-002.tiff"
CAL_VV_KEY = PREFIX + "annotation/calibration/calibration-s1d-iw-grd-vv-20261014t053204-001.xml"
CAL_VH_KEY = PREFIX + "annotation/calibration/calibration-s1d-iw-grd-vh-20261014t053204-002.xml"
NOISE_VH_KEY = PREFIX + "annotation/calibration/noise-s1d-iw-grd-vh-20261014t053204-002.xml"
NOISE_VV_KEY = PREFIX + "annotation/calibration/noise-s1d-iw-grd-vv-20261014t053204-001.xml"

HEIGHT, WIDTH = 1200, 500
GCP_ROWS = [0, 400, 800, HEIGHT - 1]
GCP_COLS = [0, 125, 250, 375, WIDTH - 1]

Mapping = Callable[[Any, Any], tuple[Any, Any]]  # (col, row) -> (lon, lat), numpy-friendly


def linear_mapping(col: Any, row: Any) -> tuple[Any, Any]:
    return 10.0 + 1.0e-4 * col + 2e-5 * row, 55.5 - 9e-5 * row + 1e-5 * col


def curved_mapping(col: Any, row: Any) -> tuple[Any, Any]:
    lon, lat = linear_mapping(col, row)
    return lon + 5e-9 * col * row, lat


def tiepoints(mapping: Mapping = linear_mapping) -> list[float]:
    values: list[float] = []
    for r in GCP_ROWS:
        for c in GCP_COLS:
            lon, lat = mapping(float(c), float(r))
            values += [float(c), float(r), 0.0, lon, lat, 0.0]
    return values


def make_dn(ship_rc: tuple[int, int] = (650, 250), sea: int = 100, ship: int = 1000) -> NDArray[np.uint16]:
    dn = np.full((HEIGHT, WIDTH), sea, dtype=np.uint16)
    dn[:4] = 0  # no-data border
    r, c = ship_rc
    dn[r : r + 3, c : c + 3] = ship
    return dn


def make_vh_dn(ship_rc: tuple[int, int] = (650, 250)) -> NDArray[np.uint16]:
    """VH: darker sea (DN 10 -> sigma0 0.01) and a ship 30 dB above it (DN 316 -> 10)."""
    return make_dn(ship_rc, sea=10, ship=316)


def make_tiff(
    dn: NDArray[np.uint16],
    mapping: Mapping = linear_mapping,
    *,
    compression: str | None = None,
    tile: tuple[int, int] | None = None,
    byteorder: Literal["<", ">"] = "<",
) -> bytes:
    buf = io.BytesIO()
    pts = tiepoints(mapping)
    kwargs: dict[str, Any] = {"tile": tile} if tile else {"rowsperstrip": 1}
    tifffile.imwrite(
        buf,
        dn,
        photometric="minisblack",
        compression=compression,
        byteorder=byteorder,
        extratags=[(33922, 12, len(pts), pts, True)],  # ModelTiepointTag, TIFF type 12 = DOUBLE
        **kwargs,
    )
    return buf.getvalue()


def noise_xml(power: float = 400.0, azimuth: float = 1.0) -> bytes:
    """Noise annotation (IPF 3): a constant range LUT (DN^2) and one swath with a constant azimuth LUT."""
    vectors = "".join(
        f"<noiseRangeVector><line>{line}</line>"
        f'<pixel count="3">0 250 {WIDTH - 1}</pixel>'
        f'<noiseRangeLut count="3">{power} {power} {power}</noiseRangeLut></noiseRangeVector>'
        for line in (0, 600, HEIGHT - 1)
    )
    return (
        '<?xml version="1.0"?><noise><noiseRangeVectorList count="3">'
        + vectors
        + '</noiseRangeVectorList><noiseAzimuthVectorList count="1"><noiseAzimuthVector><swath>IW1</swath>'
        + "<firstAzimuthLine>0</firstAzimuthLine><firstRangeSample>0</firstRangeSample>"
        + f"<lastAzimuthLine>{HEIGHT - 1}</lastAzimuthLine><lastRangeSample>{WIDTH - 1}</lastRangeSample>"
        + f'<line count="2">0 {HEIGHT - 1}</line>'
        + f'<noiseAzimuthLut count="2">{azimuth} {azimuth}</noiseAzimuthLut>'
        + "</noiseAzimuthVector></noiseAzimuthVectorList></noise>"
    ).encode()


def calibration_xml(sigma: float = 100.0) -> bytes:
    """Calibration annotation with a constant sigmaNought LUT on a sparse grid."""
    vectors = "".join(
        f"<calibrationVector><line>{line}</line>"
        f'<pixel count="3">0 250 {WIDTH - 1}</pixel>'
        f'<sigmaNought count="3">{sigma} {sigma} {sigma}</sigmaNought></calibrationVector>'
        for line in (0, 600, HEIGHT - 1)
    )
    return (
        '<?xml version="1.0"?><calibration><calibrationVectorList count="3">'
        + vectors
        + "</calibrationVectorList></calibration>"
    ).encode()


def footprint(mapping: Mapping = linear_mapping) -> list[tuple[float, float]]:
    corners = [(0, 0), (WIDTH - 1, 0), (WIDTH - 1, HEIGHT - 1), (0, HEIGHT - 1), (0, 0)]
    return [tuple(float(x) for x in mapping(float(c), float(r))) for c, r in corners]  # type: ignore[misc]


def region_ring(
    mapping: Mapping = linear_mapping, cols: tuple[int, int] = (100, 400), rows: tuple[int, int] = (400, 800)
) -> list[tuple[float, float]]:
    (c0, c1), (r0, r1) = cols, rows
    corners = [(c0, r0), (c1, r0), (c1, r1), (c0, r1), (c0, r0)]
    return [tuple(float(x) for x in mapping(float(c), float(r))) for c, r in corners]  # type: ignore[misc]


def upload_product(
    client: Any,
    tiff: bytes,
    cal: bytes | None = None,
    bucket: str = "eodata",
    vh: bytes | None = None,
    with_vh: bool = True,
    noise: bytes | None = None,
) -> None:
    client.put_object(Bucket=bucket, Key=VV_KEY, Body=tiff)
    if with_vh:
        client.put_object(Bucket=bucket, Key=VH_KEY, Body=vh if vh is not None else make_tiff(make_vh_dn()))
        client.put_object(Bucket=bucket, Key=CAL_VH_KEY, Body=calibration_xml())
        if noise is not None:
            client.put_object(Bucket=bucket, Key=NOISE_VH_KEY, Body=noise)
    client.put_object(Bucket=bucket, Key=CAL_VV_KEY, Body=cal if cal is not None else calibration_xml())
    client.put_object(Bucket=bucket, Key=NOISE_VV_KEY, Body=noise if noise is not None else b"<noise/>")
