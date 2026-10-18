"""Land mask backed by land polygons (any shapely geometries; OSM land polygons are loaded elsewhere).

Two views of the same polygons:
- `water_mask(raster)`: protocol method. True only for open water: land AND the near-shore band are False.
- `not_land_mask(raster)`: True everywhere except land. The near-shore band stays valid, so a detector run on
  this mask still finds near-shore ships, which `classify` then flags as `NEAR_SHORE`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import shapely
from numpy.typing import NDArray
from shapely import STRtree
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry

from contracts.protocols import LonLat, SceneRaster, ShoreClass
from pipeline.algorithms.geo import (
    Affine6,
    lonlat_to_pixel,
    meters_per_degree,
    raster_center_lat,
)

_ROW_BLOCK = 512


def rasterize(geometry: BaseGeometry, transform: Affine6, shape_rc: tuple[int, int]) -> NDArray[np.bool_]:
    """Even-odd scanline fill of `geometry` (lon/lat) onto a raster grid; pixel centre inside means True.

    Vectorised over polygon edges and rows, so cost scales with coastline length, not raster area.
    """
    n_rows, n_cols = shape_rc
    out = np.zeros((n_rows, n_cols), dtype=bool)
    if geometry.is_empty:
        return out

    x0l: list[NDArray[np.float64]] = []
    y0l: list[NDArray[np.float64]] = []
    x1l: list[NDArray[np.float64]] = []
    y1l: list[NDArray[np.float64]] = []
    for part in shapely.get_parts(geometry):
        if part.geom_type != "Polygon":
            continue
        for ring in [part.exterior, *part.interiors]:
            xy = np.asarray(ring.coords, dtype=np.float64)
            rows, cols = lonlat_to_pixel(transform, xy[:, 0], xy[:, 1])
            x0l.append(cols[:-1])
            y0l.append(rows[:-1])
            x1l.append(cols[1:])
            y1l.append(rows[1:])
    if not x0l:
        return out
    x0, y0, x1, y1 = (np.concatenate(a) for a in (x0l, y0l, x1l, y1l))

    # A scanline at row r sits at y = r + 0.5; an edge crosses it when min(y) <= y < max(y).
    ylo, yhi = np.minimum(y0, y1), np.maximum(y0, y1)
    first = np.maximum(np.ceil(ylo - 0.5), 0).astype(np.int64)
    last = np.minimum(np.ceil(yhi - 0.5) - 1, n_rows - 1).astype(np.int64)
    n_cross = np.where((y0 != y1) & (last >= first), last - first + 1, 0)
    total = int(n_cross.sum())
    if total == 0:
        return out
    edge = np.repeat(np.arange(n_cross.size), n_cross)
    offs = np.arange(total) - np.repeat(np.cumsum(n_cross) - n_cross, n_cross)
    cross_rows = first[edge] + offs
    t = (cross_rows + 0.5 - y0[edge]) / (y1[edge] - y0[edge])
    cross_x = x0[edge] + t * (x1[edge] - x0[edge])
    # First pixel column whose centre is at or right of the crossing.
    cross_cols = np.clip(np.ceil(cross_x - 0.5), 0, n_cols).astype(np.int64)

    order = np.argsort(cross_rows, kind="stable")
    cross_rows, cross_cols = cross_rows[order], cross_cols[order]
    for b0 in range(0, n_rows, _ROW_BLOCK):
        b1 = min(n_rows, b0 + _ROW_BLOCK)
        lo, hi = np.searchsorted(cross_rows, [b0, b1])
        if lo == hi:
            continue
        flat = (cross_rows[lo:hi] - b0) * (n_cols + 1) + cross_cols[lo:hi]
        toggles = np.bincount(flat, minlength=(b1 - b0) * (n_cols + 1)) & 1
        fill = np.cumsum(toggles.reshape(b1 - b0, n_cols + 1).astype(np.uint8), axis=1, dtype=np.uint8) & 1
        out[b0:b1] = fill[:, :n_cols].astype(bool)
    return out


class PolygonLandMask:
    """`LandMask` implementation over land polygons given as shapely geometries (lon/lat).

    Three zones around the land: the exclusion zone (land plus `exclusion_m` around it) is ignored like land,
    the near-shore band (up to `near_shore_m` from land) is kept but flagged, the rest is open water. Radar
    returns right at a coast (quays, piers, breakwaters, moored boats) are mostly not ships, hence the zone.
    """

    def __init__(
        self,
        land: BaseGeometry | Iterable[BaseGeometry],
        near_shore_m: float = 300.0,
        exclusion_m: float = 0.0,
    ) -> None:
        geoms = [land] if isinstance(land, BaseGeometry) else list(land)
        self.near_shore_m = max(near_shore_m, exclusion_m)  # the flagged band includes the ignored zone
        self.exclusion_m = exclusion_m
        self._geoms = [g for g in geoms if not g.is_empty]
        self._tree = STRtree(self._geoms) if self._geoms else None

    @classmethod
    def from_geojson(
        cls, path: str | Path, near_shore_m: float = 300.0, exclusion_m: float = 0.0
    ) -> PolygonLandMask:
        data = json.loads(Path(path).read_text())
        features = data["features"] if data.get("type") == "FeatureCollection" else [data]
        geoms = [shape(f.get("geometry", f)) for f in features]
        return cls(geoms, near_shore_m, exclusion_m)

    def water_mask(self, raster: SceneRaster) -> NDArray[np.bool_]:
        """Open water only: excludes land and the near-shore band."""
        _, near = self._land_and_near(raster)
        return ~near

    def not_land_mask(self, raster: SceneRaster) -> NDArray[np.bool_]:
        """Everything except land and its exclusion zone; the near-shore band stays valid (flagged)."""
        excluded, _ = self._land_and_near(raster)
        return ~excluded

    def classify(self, position: LonLat) -> ShoreClass:
        if self._tree is None:
            return ShoreClass.OPEN_WATER
        lon, lat = position
        m_lon, m_lat = meters_per_degree(lat)
        margin_m = max(self.near_shore_m, 1.0) * 1.5
        dx, dy = margin_m / m_lon, margin_m / m_lat
        window = box(lon - dx, lat - dy, lon + dx, lat + dy)
        idx = self._tree.query(window)
        if len(idx) == 0:
            return ShoreClass.OPEN_WATER
        clipped = shapely.union_all([shapely.clip_by_rect(self._geoms[i], *window.bounds) for i in idx])
        if clipped.is_empty:
            return ShoreClass.OPEN_WATER
        local = _to_local(clipped, lon, lat, m_lon, m_lat)
        origin = shapely.Point(0.0, 0.0)
        if local.contains(origin) or local.touches(origin):
            return ShoreClass.ON_LAND
        distance = local.distance(origin)
        if distance <= self.exclusion_m:
            return ShoreClass.ON_LAND  # ignored like land
        if distance <= self.near_shore_m:
            return ShoreClass.NEAR_SHORE
        return ShoreClass.OPEN_WATER

    def _land_and_near(self, raster: SceneRaster) -> tuple[NDArray[np.bool_], NDArray[np.bool_]]:
        """Pixels ignored (land plus the exclusion zone) and pixels in the near-shore band (a superset)."""
        n_rows, n_cols = raster.sigma0_vv.shape
        shape_rc = (n_rows, n_cols)
        if self._tree is None:
            empty = np.zeros(shape_rc, dtype=bool)
            return empty, empty
        lat0 = raster_center_lat(raster.transform, shape_rc)
        m_lon, m_lat = meters_per_degree(lat0)

        corner_rows = np.array([0.0, 0.0, n_rows, n_rows])
        corner_cols = np.array([0.0, n_cols, 0.0, n_cols])
        a, b, c, d, e, f = raster.transform
        lons = a * corner_cols + b * corner_rows + c
        lats = d * corner_cols + e * corner_rows + f
        pad_x, pad_y = 2 * self.near_shore_m / m_lon, 2 * self.near_shore_m / m_lat
        bounds = (lons.min() - pad_x, lats.min() - pad_y, lons.max() + pad_x, lats.max() + pad_y)

        idx = self._tree.query(box(*bounds))
        if len(idx) == 0:
            empty = np.zeros(shape_rc, dtype=bool)
            return empty, empty
        land = shapely.union_all([shapely.clip_by_rect(self._geoms[i], *bounds) for i in idx])
        land_px = rasterize(land, raster.transform, shape_rc)
        if self.near_shore_m <= 0:
            return land_px, land_px
        local = _to_local(land, lons.mean(), lat0, m_lon, m_lat)

        def zone(metres: float) -> NDArray[np.bool_]:
            if metres <= 0:
                return land_px
            buffered = _from_local(local.buffer(metres), lons.mean(), lat0, m_lon, m_lat)
            return rasterize(buffered, raster.transform, shape_rc)

        return zone(self.exclusion_m), zone(self.near_shore_m)


def _to_local(geom: BaseGeometry, lon0: float, lat0: float, m_lon: float, m_lat: float) -> BaseGeometry:
    """Lon/lat geometry to local metres east/north of (lon0, lat0)."""
    return shapely.transform(
        geom, lambda xy: np.column_stack([(xy[:, 0] - lon0) * m_lon, (xy[:, 1] - lat0) * m_lat])
    )


def _from_local(geom: BaseGeometry, lon0: float, lat0: float, m_lon: float, m_lat: float) -> BaseGeometry:
    return shapely.transform(
        geom, lambda xy: np.column_stack([xy[:, 0] / m_lon + lon0, xy[:, 1] / m_lat + lat0])
    )
