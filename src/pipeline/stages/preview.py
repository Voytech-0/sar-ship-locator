"""Scene preview: the VV band as a north-up lat/lon PNG, for the review website.

Sentinel-1 GRD is in slant-geometry pixel space, so the raster is block-averaged, each block is placed at its
geolocated position and the result is resampled onto a regular lon/lat grid (Leaflet `ImageOverlay` bounds).
Alpha is 0 outside the Region.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import distance_transform_edt

from contracts.protocols import ObjectStore, SceneRaster
from pipeline.stages.chips import png_bytes, to_8bit

BLOCK_PX = 3  # source pixels averaged per side (about 30 m)
OUT_M = 30.0  # output pixel size
M_PER_DEG_LAT = 111_320.0


@dataclass(frozen=True)
class Preview:
    png: bytes
    bounds: tuple[float, float, float, float]  # south, west, north, east


def preview_key(scene_id: str) -> str:
    return f"scenes/{scene_id}/preview.png"


def make_preview(raster: SceneRaster) -> Preview | None:
    k = BLOCK_PX
    rows, cols = (s // k * k for s in raster.sigma0_vv.shape)
    vv = raster.sigma0_vv[:rows, :cols]
    ok = np.isfinite(vv)
    shape = (rows // k, k, cols // k, k)
    n = ok.reshape(shape).sum(axis=(1, 3))
    mean = np.where(ok, vv, 0.0).reshape(shape).sum(axis=(1, 3)) / np.maximum(n, 1)
    valid = n > 0
    if not valid.any():
        return None

    r = np.arange(rows // k) * k + (k - 1) / 2
    c = np.arange(cols // k) * k + (k - 1) / 2
    if raster.geolocate is not None:
        lon, lat = raster.geolocate(r[:, None], c[None, :])
    else:
        a, b, tx, d, e, ty = raster.transform
        lon = a * c[None, :] + b * r[:, None] + tx
        lat = d * c[None, :] + e * r[:, None] + ty
    lon, lat = lon[valid], lat[valid]
    south, north, west, east = lat.min(), lat.max(), lon.min(), lon.max()

    dlat = OUT_M / M_PER_DEG_LAT
    dlon = dlat / np.cos(np.radians((south + north) / 2))
    h, w = int((north - south) / dlat) + 1, int((east - west) / dlon) + 1
    iy = np.clip(((north - lat) / dlat).astype(int), 0, h - 1)
    ix = np.clip(((lon - west) / dlon).astype(int), 0, w - 1)
    total = np.zeros((h, w))
    count = np.zeros((h, w))
    np.add.at(total, (iy, ix), mean[valid])
    np.add.at(count, (iy, ix), 1.0)

    # fill the one-cell holes a rotated block lattice leaves; farther empty cells stay transparent
    dist, (fy, fx) = distance_transform_edt(count == 0, return_indices=True)
    filled = (count == 0) & (dist <= 1.5)
    value = np.where(count > 0, total / np.maximum(count, 1), (total / np.maximum(count, 1))[fy, fx])
    opaque = (count > 0) | filled

    img = np.stack([to_8bit(value.astype(np.float32)), opaque.astype(np.uint8) * 255], axis=-1)
    return Preview(png_bytes(img), (float(south), float(west), float(north), float(east)))


def save_preview(store: ObjectStore, scene_id: str, preview: Preview) -> None:
    store.put(preview_key(scene_id), preview.png, "image/png")
    south, west, north, east = preview.bounds
    meta = {"bounds": [[south, west], [north, east]], "pixel_m": OUT_M}
    store.put(f"scenes/{scene_id}/preview.json", json.dumps(meta).encode(), "application/json")
