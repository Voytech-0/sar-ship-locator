"""Cell-averaging CFAR ship detector for Sentinel-1 sigma0 rasters.

For every valid pixel the background level is the mean of valid pixels in a square ring (guard window
excluded). A pixel is a hit when its intensity exceeds `threshold * background`. For multi-look intensity
with equivalent number of looks `enl`, clutter is gamma distributed, so the ratio of a clutter pixel to a
background estimated from N cells follows an F(2*enl, 2*enl*N) distribution; the threshold is that
distribution's upper quantile at `pfa`. Hits are merged into clusters, which become Detections.

Known limitations: the background is contaminated by neighbouring bright targets, hits come from the brightest
pixels so the length estimate is a lower bound, and pixels correlated by the sensor's oversampling make the
real false alarm rate somewhat different from `pfa`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.ndimage import uniform_filter
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree
from scipy.stats import f as f_dist

from contracts.protocols import DetectionCandidate, SceneRaster
from pipeline.algorithms.geo import (
    pixel_metric_matrix,
    pixel_to_lonlat,
    raster_center_lat,
)

_VH_RING_INNER_PX = 20  # the sea ring starts this far from the box, outside the ship's wake and sidelobes
_VH_RING_OUTER_PX = 50


def vh_contrast_db(vh: NDArray[np.float32], box: tuple[int, int, int, int]) -> float | None:
    """Peak VH inside `box` over the median VH of the ring around it, in dB.

    A ship stands out in VH (dark sea, no sea-state edges: a median 26 dB against 7 dB for unmatched
    clusters on 9 real Scenes); None when the box or the ring has no valid pixel.
    """
    r0, c0, r1, c1 = box
    peak = np.nanmax(vh[r0:r1, c0:c1]) if vh[r0:r1, c0:c1].size else np.nan
    h, w = vh.shape
    top, bottom = max(r0 - _VH_RING_OUTER_PX, 0), min(r1 + _VH_RING_OUTER_PX, h)
    left, right = max(c0 - _VH_RING_OUTER_PX, 0), min(c1 + _VH_RING_OUTER_PX, w)
    ring = vh[top:bottom, left:right].copy()
    ring[
        max(r0 - _VH_RING_INNER_PX, 0) - top : r1 + _VH_RING_INNER_PX - top,
        max(c0 - _VH_RING_INNER_PX, 0) - left : c1 + _VH_RING_INNER_PX - left,
    ] = np.nan
    if not np.isfinite(ring).any():
        return None
    background = np.nanmedian(ring)
    if not (np.isfinite(peak) and np.isfinite(background) and peak > 0 and background > 0):
        return None
    return float(10 * np.log10(peak / background))


class TooManyHitsError(ValueError):
    """The raster produced an implausible number of hit pixels (e.g. land was not masked)."""


class CaCfarDetector:
    """`Detector` implementation, name 'ca-cfar'.

    `version` labels a parameter set: bump it in the Region config when parameters change, so Detections
    produced with different settings stay distinguishable.
    """

    def __init__(
        self,
        guard_cells: int = 8,
        background_cells: int = 24,
        pfa: float = 1e-6,
        min_cluster_pixels: int = 3,
        enl: float = 4.4,
        merge_distance_px: int = 2,
        min_valid_fraction: float = 0.25,
        length_bias_m: float = 0.0,
        min_length_m: float = 0.0,
        min_confidence: float = 0.0,
        min_vh_contrast_db: float | None = None,
        chunk_rows: int = 1024,
        max_hits: int = 2_000_000,
        version: str = "1",
    ) -> None:
        if guard_cells < 0 or background_cells < 1:
            raise ValueError("guard_cells must be >= 0 and background_cells >= 1")
        if not 0 < pfa < 1:
            raise ValueError("pfa must be in (0, 1)")
        self.name = "ca-cfar"
        self.version = version
        self.guard_cells = guard_cells
        self.background_cells = background_cells
        self.pfa = pfa
        self.min_cluster_pixels = min_cluster_pixels
        self.enl = enl
        self.merge_distance_px = merge_distance_px
        self.min_valid_fraction = min_valid_fraction
        if length_bias_m < 0:
            raise ValueError("length_bias_m must be >= 0")
        self.length_bias_m = length_bias_m
        self.min_length_m = min_length_m
        self.min_confidence = min_confidence
        self.min_vh_contrast_db = min_vh_contrast_db
        self.chunk_rows = chunk_rows
        self.max_hits = max_hits
        self._half_in = guard_cells
        self._half_out = guard_cells + background_cells
        self._size_in = 2 * self._half_in + 1
        self._size_out = 2 * self._half_out + 1
        self.n_reference = self._size_out**2 - self._size_in**2
        self.threshold = float(f_dist.isf(pfa, 2 * enl, 2 * enl * self.n_reference))

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> CaCfarDetector:
        """Build from the `detector.params` block of a Region config."""
        known: dict[str, type] = {
            "guard_cells": int,
            "background_cells": int,
            "pfa": float,
            "min_cluster_pixels": int,
            "enl": float,
            "merge_distance_px": int,
            "min_valid_fraction": float,
            "length_bias_m": float,
            "min_length_m": float,
            "min_confidence": float,
            "min_vh_contrast_db": float,
            "version": str,
        }
        unknown = set(params) - set(known)
        if unknown:
            raise ValueError(f"unknown ca-cfar parameters: {sorted(unknown)}")
        return cls(**{k: known[k](v) for k, v in params.items()})

    def detect(self, raster: SceneRaster) -> Sequence[DetectionCandidate]:
        x = raster.sigma0_vv
        valid = np.isfinite(x)
        if raster.water_mask is not None:
            valid &= raster.water_mask
        hit_rows, hit_cols, hit_vals, hit_ratio = self._find_hits(x, valid)
        if hit_rows.size == 0:
            return []
        return self._cluster(raster, hit_rows, hit_cols, hit_vals, hit_ratio)

    def _find_hits(
        self, x: NDArray[np.float32], valid: NDArray[np.bool_]
    ) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.float64], NDArray[np.float64]]:
        rows = x.shape[0]
        halo = self._half_out
        size_in2, size_out2 = float(self._size_in**2), float(self._size_out**2)
        min_count = self.min_valid_fraction * self.n_reference
        out_r: list[NDArray[np.int64]] = []
        out_c: list[NDArray[np.int64]] = []
        out_v: list[NDArray[np.float64]] = []
        out_ratio: list[NDArray[np.float64]] = []
        total = 0
        for r0 in range(0, rows, self.chunk_rows):
            r1 = min(rows, r0 + self.chunk_rows)
            a0, a1 = max(0, r0 - halo), min(rows, r1 + halo)
            v = valid[a0:a1]
            lo, hi = r0 - a0, r1 - a0
            if not v[lo:hi].any():
                continue
            xs = np.where(v, x[a0:a1], np.float32(0)).astype(np.float32, copy=False)
            vf = v.astype(np.float32)
            bg_sum = (
                uniform_filter(xs, size=self._size_out, mode="constant") * size_out2
                - uniform_filter(xs, size=self._size_in, mode="constant") * size_in2
            )[lo:hi]
            bg_cnt = (
                uniform_filter(vf, size=self._size_out, mode="constant") * size_out2
                - uniform_filter(vf, size=self._size_in, mode="constant") * size_in2
            )[lo:hi]
            xc = xs[lo:hi]
            hit = v[lo:hi] & (bg_cnt >= min_count) & (bg_sum > 0) & (xc * bg_cnt > self.threshold * bg_sum)
            rr, cc = np.nonzero(hit)
            if rr.size == 0:
                continue
            total += rr.size
            if total > self.max_hits:
                raise TooManyHitsError(f"more than {self.max_hits} hit pixels; is land masked?")
            out_r.append(rr.astype(np.int64) + r0)
            out_c.append(cc.astype(np.int64))
            out_v.append(xc[rr, cc].astype(np.float64))
            out_ratio.append((xc[rr, cc] * bg_cnt[rr, cc] / bg_sum[rr, cc]).astype(np.float64))
        if not out_r:
            empty_i = np.empty(0, dtype=np.int64)
            empty_f = np.empty(0, dtype=np.float64)
            return empty_i, empty_i, empty_f, empty_f
        return (
            np.concatenate(out_r),
            np.concatenate(out_c),
            np.concatenate(out_v),
            np.concatenate(out_ratio),
        )

    def _cluster(
        self,
        raster: SceneRaster,
        rows: NDArray[np.int64],
        cols: NDArray[np.int64],
        vals: NDArray[np.float64],
        ratio: NDArray[np.float64],
    ) -> list[DetectionCandidate]:
        n = rows.size
        pts = np.column_stack([rows, cols])
        pairs = cKDTree(pts).query_pairs(r=self.merge_distance_px, p=np.inf, output_type="ndarray")
        if pairs.size:
            graph = coo_matrix((np.ones(len(pairs), dtype=np.int8), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
            n_comp, labels_raw = connected_components(graph, directed=False)
            labels = labels_raw.astype(np.intp)
        else:
            n_comp, labels = n, np.arange(n, dtype=np.intp)
        counts = np.bincount(labels, minlength=n_comp)
        keep_label = counts >= self.min_cluster_pixels
        if not keep_label.any():
            return []

        sel = keep_label[labels]
        rows, cols, vals, ratio, labels = rows[sel], cols[sel], vals[sel], ratio[sel], labels[sel]
        labels = np.unique(labels, return_inverse=True)[1].astype(np.intp)  # compact to 0..k-1
        k = int(labels.max()) + 1
        fcounts = np.bincount(labels, minlength=k).astype(np.float64)

        rf, cf = rows.astype(np.float64), cols.astype(np.float64)
        w_sum = np.bincount(labels, weights=vals, minlength=k)
        row_c = np.bincount(labels, weights=vals * rf, minlength=k) / w_sum
        col_c = np.bincount(labels, weights=vals * cf, minlength=k) / w_sum

        order = np.argsort(labels, kind="stable")
        starts = np.concatenate([[0], np.flatnonzero(np.diff(labels[order])) + 1])
        row0 = np.minimum.reduceat(rows[order], starts)
        row1 = np.maximum.reduceat(rows[order], starts) + 1
        col0 = np.minimum.reduceat(cols[order], starts)
        col1 = np.maximum.reduceat(cols[order], starts) + 1
        peak = np.maximum.reduceat(ratio[order], starts)

        # Length: principal axis of the hit pixels in metres, as a uniform-rectangle equivalent.
        lat0 = raster_center_lat(raster.transform, raster.sigma0_vv.shape)
        m = pixel_metric_matrix(raster.transform, lat0)
        east = m[0, 0] * cf + m[0, 1] * rf
        north = m[1, 0] * cf + m[1, 1] * rf
        e_c = east - (np.bincount(labels, weights=east, minlength=k) / fcounts)[labels]
        n_c = north - (np.bincount(labels, weights=north, minlength=k) / fcounts)[labels]
        sxx = np.bincount(labels, weights=e_c * e_c, minlength=k) / fcounts
        syy = np.bincount(labels, weights=n_c * n_c, minlength=k) / fcounts
        sxy = np.bincount(labels, weights=e_c * n_c, minlength=k) / fcounts
        lam = (sxx + syy) / 2 + np.sqrt(((sxx - syy) / 2) ** 2 + sxy**2)
        pixel_m = float(np.sqrt(abs(np.linalg.det(m))))
        # The extent of a cluster includes the sensor's point spread: against AIS lengths of 59 matched ships
        # the raw estimate is about 20 m too long, so `length_bias_m` is subtracted (never below one pixel).
        length = np.maximum(np.sqrt(12 * lam + pixel_m**2) - self.length_bias_m, pixel_m)

        confidence = np.clip(1.0 - self.threshold / peak, 0.0, 1.0)
        if raster.geolocate is not None:  # accurate GCP mapping; takes pixel-centre indices
            lons, lats = raster.geolocate(row_c, col_c)
        else:
            lons, lats = pixel_to_lonlat(raster.transform, row_c + 0.5, col_c + 0.5)

        vh = raster.sigma0_vh
        result: list[DetectionCandidate] = []
        for i in range(k):
            if length[i] < self.min_length_m or confidence[i] < self.min_confidence:
                continue
            box = (int(row0[i]), int(col0[i]), int(row1[i]), int(col1[i]))
            contrast = None if vh is None else vh_contrast_db(vh, box)
            if (
                self.min_vh_contrast_db is not None
                and contrast is not None
                and contrast < self.min_vh_contrast_db
            ):
                continue
            result.append(
                DetectionCandidate(
                    position=(float(lons[i]), float(lats[i])),
                    confidence=float(confidence[i]),
                    length_m=float(length[i]),
                    pixel_box=box,
                    vh_contrast_db=contrast,
                )
            )
        result.sort(key=lambda d: d.pixel_box)
        return result
