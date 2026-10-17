"""Small geodesy helpers shared by the algorithms: local metric scales and affine handling.

Distances here are local equirectangular approximations. Over a Region a few tens of kilometres wide the
error is well below 2 %, which is far under the 500 m match gate's own uncertainty.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

Affine6 = tuple[float, float, float, float, float, float]


def meters_per_degree(lat_deg: float) -> tuple[float, float]:
    """Return (metres per degree of longitude, metres per degree of latitude) at `lat_deg` (WGS 84)."""
    phi = math.radians(lat_deg)
    m_lat = 111_132.92 - 559.82 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi) - 0.0023 * math.cos(6 * phi)
    m_lon = 111_412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi) + 0.118 * math.cos(5 * phi)
    return m_lon, m_lat


def pixel_to_lonlat(
    transform: Affine6, rows: NDArray[np.float64], cols: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Map fractional pixel coordinates (row, col) to (lon, lat). Pixel centres are at index + 0.5."""
    a, b, c, d, e, f = transform
    return a * cols + b * rows + c, d * cols + e * rows + f


def lonlat_to_pixel(
    transform: Affine6, lons: NDArray[np.float64], lats: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Inverse of `pixel_to_lonlat`: returns (rows, cols), fractional."""
    a, b, c, d, e, f = transform
    det = a * e - b * d
    if det == 0:
        raise ValueError("degenerate affine transform")
    x = lons - c
    y = lats - f
    cols = (e * x - b * y) / det
    rows = (-d * x + a * y) / det
    return rows, cols


def pixel_metric_matrix(transform: Affine6, lat_deg: float) -> NDArray[np.float64]:
    """2x2 matrix mapping a (col, row) pixel step to an (east, north) step in metres."""
    a, b, _, d, e, _ = transform
    m_lon, m_lat = meters_per_degree(lat_deg)
    return np.array([[a * m_lon, b * m_lon], [d * m_lat, e * m_lat]], dtype=np.float64)


def raster_center_lat(transform: Affine6, shape: tuple[int, int]) -> float:
    rows, cols = shape
    _, lat = pixel_to_lonlat(transform, np.array([rows / 2.0]), np.array([cols / 2.0]))
    return float(lat[0])
