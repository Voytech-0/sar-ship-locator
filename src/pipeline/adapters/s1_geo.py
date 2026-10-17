"""Geolocation of Sentinel-1 GRD pixels from the GCP grid stored in the measurement TIFF.

GRD products carry no CRS, only a regular grid of ground control points (GeoTIFF `ModelTiepointTag`,
six doubles per point: column, row, 0, lon, lat, height). Reference geolocation is bilinear interpolation
over that grid, the same method SNAP uses.

Pixel coordinates are pixel-centre indices, so `affine(col, row)` is the centre of pixel (row, col). That is
half a pixel (5 m) off the rasterio corner convention.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import shapely
from numpy.typing import NDArray
from scipy.interpolate import RegularGridInterpolator
from shapely.geometry import Polygon

from contracts.protocols import LonLat, PermanentError

PIXEL_SPACING_M = 10.0  # IW GRDH
_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LON_EQ = 111_320.0

Affine = tuple[float, float, float, float, float, float]


@dataclass(frozen=True)
class AffineFit:
    transform: Affine
    max_error_px: float
    mean_error_px: float
    max_error_m: float


class GcpGrid:
    def __init__(self, tiepoints: NDArray[np.float64]) -> None:
        pts = np.asarray(tiepoints, dtype=np.float64).reshape(-1, 6)
        cols = np.unique(pts[:, 0])
        rows = np.unique(pts[:, 1])
        if len(pts) != len(cols) * len(rows) or len(cols) < 2 or len(rows) < 2:
            raise PermanentError(f"GCPs do not form a regular grid ({len(pts)} points)")
        lon = np.empty((len(rows), len(cols)))
        lat = np.empty((len(rows), len(cols)))
        ri = np.searchsorted(rows, pts[:, 1])
        ci = np.searchsorted(cols, pts[:, 0])
        lon[ri, ci] = pts[:, 3]
        lat[ri, ci] = pts[:, 4]
        self._lon = RegularGridInterpolator((rows, cols), lon, bounds_error=False, fill_value=None)
        self._lat = RegularGridInterpolator((rows, cols), lat, bounds_error=False, fill_value=None)

    def lonlat(
        self, rows: NDArray[np.float64], cols: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Bilinear lon/lat of pixels (rows and cols broadcast); extrapolates outside the grid."""
        r, c = np.broadcast_arrays(np.asarray(rows, dtype=np.float64), np.asarray(cols, dtype=np.float64))
        pts = np.stack([r.ravel(), c.ravel()], axis=1)
        return self._lon(pts).reshape(r.shape), self._lat(pts).reshape(r.shape)

    def region_window(
        self, polygon: Polygon, height: int, width: int, stride: int = 25, margin: int = 16
    ) -> tuple[int, int, int, int] | None:
        """Pixel window (row0, row1, col0, col1), half-open and clipped to the image, covering `polygon`.

        Found by testing a coarse pixel grid against the polygon, so it is exact to within `stride + margin`
        pixels. None if no sampled pixel lies inside the polygon.
        """
        rows = np.arange(0, height, stride, dtype=np.float64)
        cols = np.arange(0, width, stride, dtype=np.float64)
        lon, lat = self.lonlat(rows[:, None], cols[None, :])
        inside = shapely.contains_xy(polygon, lon, lat)
        if not inside.any():
            return None
        ri, ci = np.nonzero(inside)
        pad = stride + margin
        return (
            max(0, int(rows[ri.min()]) - pad),
            min(height, int(rows[ri.max()]) + pad + 1),
            max(0, int(cols[ci.min()]) - pad),
            min(width, int(cols[ci.max()]) + pad + 1),
        )

    def region_mask(
        self, polygon: Polygon, window: tuple[int, int, int, int], step: int = 4
    ) -> NDArray[np.bool_]:
        """True where a window pixel lies inside `polygon`. Evaluated every `step` pixels and repeated, so the
        boundary is accurate to `step` pixels (40 m)."""
        r0, r1, c0, c1 = window
        rows = np.arange(r0, r1, step, dtype=np.float64)
        cols = np.arange(c0, c1, step, dtype=np.float64)
        lon, lat = self.lonlat(rows[:, None], cols[None, :])
        coarse = shapely.contains_xy(polygon, lon, lat)
        full = np.repeat(np.repeat(coarse, step, axis=0), step, axis=1)
        return full[: r1 - r0, : c1 - c0]

    def fit_affine(self, window: tuple[int, int, int, int], sample_step: int = 25) -> AffineFit:
        """Least-squares affine pixel->lon/lat over `window`, with its error versus bilinear GCPs.

        The affine maps window-relative (col, row) to (lon, lat).
        """
        r0, r1, c0, c1 = window
        rows = np.arange(r0, r1, sample_step, dtype=np.float64)
        cols = np.arange(c0, c1, sample_step, dtype=np.float64)
        lon, lat = self.lonlat(rows[:, None], cols[None, :])
        rr, cc = np.broadcast_arrays(rows[:, None] - r0, cols[None, :] - c0)
        design = np.stack([cc.ravel(), rr.ravel(), np.ones(cc.size)], axis=1)
        (a, b, c), *_ = np.linalg.lstsq(design, lon.ravel(), rcond=None)
        (d, e, f), *_ = np.linalg.lstsq(design, lat.ravel(), rcond=None)
        dlon = design @ np.array([a, b, c]) - lon.ravel()
        dlat = design @ np.array([d, e, f]) - lat.ravel()
        err_m = np.hypot(dlon * _M_PER_DEG_LON_EQ * np.cos(np.radians(lat.ravel())), dlat * _M_PER_DEG_LAT)
        err_px = err_m / PIXEL_SPACING_M
        return AffineFit(
            transform=(float(a), float(b), float(c), float(d), float(e), float(f)),
            max_error_px=float(err_px.max()),
            mean_error_px=float(err_px.mean()),
            max_error_m=float(err_m.max()),
        )


def ring_to_polygon(ring: Sequence[LonLat]) -> Polygon:
    poly = Polygon(ring)
    if not poly.is_valid or poly.is_empty or not math.isfinite(poly.area):
        raise PermanentError("invalid region polygon")
    return poly
