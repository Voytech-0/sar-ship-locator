"""Persist a fetched `SceneRaster` between the `fetch` and `detect` stages (layout: contracts/storage.md).

Sentinel-1 GRD has no CRS, so the raster is stored as a float32 array plus a coarse lon/lat lattice sampled
from the fetcher's accurate `geolocate`. Bilinear interpolation of that lattice rebuilds `geolocate`.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from typing import cast

import numpy as np
from numpy.typing import NDArray
from scipy.interpolate import RegularGridInterpolator

from contracts.protocols import ObjectStore, PermanentError, SceneRaster

LATTICE_STEP_PX = 32


def raster_key(scene_id: str) -> str:
    return f"scenes/{scene_id}/sigma0.npy"


def _keys(scene_id: str) -> list[str]:
    return [raster_key(scene_id), f"scenes/{scene_id}/geo.npz", f"scenes/{scene_id}/meta.json"]


def _axis(n: int) -> NDArray[np.float64]:
    axis = np.arange(0, n, LATTICE_STEP_PX, dtype=np.float64)
    return axis if axis[-1] == n - 1 else np.append(axis, float(n - 1))


def save_raster(store: ObjectStore, raster: SceneRaster) -> str:
    rows, cols = raster.sigma0_vv.shape
    buf = io.BytesIO()
    np.save(buf, raster.sigma0_vv.astype(np.float32, copy=False), allow_pickle=False)
    store.put(raster_key(raster.scene_id), buf.getvalue(), "application/octet-stream")

    ra, ca = _axis(rows), _axis(cols)
    if raster.geolocate is not None:
        lon, lat = raster.geolocate(ra[:, None], ca[None, :])
    else:
        a, b, c, d, e, f = raster.transform
        lon = a * ca[None, :] + b * ra[:, None] + c
        lat = d * ca[None, :] + e * ra[:, None] + f
    geo = io.BytesIO()
    np.savez(
        geo,
        rows=ra,
        cols=ca,
        lon=np.broadcast_to(lon, (len(ra), len(ca))),
        lat=np.broadcast_to(lat, (len(ra), len(ca))),
    )
    store.put(f"scenes/{raster.scene_id}/geo.npz", geo.getvalue(), "application/octet-stream")

    meta = {
        "scene_id": raster.scene_id,
        "acquired_at": raster.acquired_at.isoformat(),
        "shape": [rows, cols],
        "transform": list(raster.transform),
        "bytes_downloaded": raster.bytes_downloaded,
    }
    store.put(f"scenes/{raster.scene_id}/meta.json", json.dumps(meta).encode(), "application/json")
    return raster_key(raster.scene_id)


def load_raster(store: ObjectStore, scene_id: str) -> SceneRaster:
    try:
        sigma0 = np.load(io.BytesIO(store.get(raster_key(scene_id))), allow_pickle=False)
        geo = np.load(io.BytesIO(store.get(f"scenes/{scene_id}/geo.npz")), allow_pickle=False)
        meta = json.loads(store.get(f"scenes/{scene_id}/meta.json"))
    except FileNotFoundError as exc:
        raise PermanentError(
            f"{scene_id}: stored raster is missing (was it deleted or never fetched?)"
        ) from exc

    lon_i = RegularGridInterpolator(
        (geo["rows"], geo["cols"]), geo["lon"], bounds_error=False, fill_value=None
    )
    lat_i = RegularGridInterpolator(
        (geo["rows"], geo["cols"]), geo["lat"], bounds_error=False, fill_value=None
    )

    def geolocate(
        rows: NDArray[np.float64], cols: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        r, c = np.broadcast_arrays(np.asarray(rows, dtype=np.float64), np.asarray(cols, dtype=np.float64))
        pts = np.stack([r.ravel(), c.ravel()], axis=1)
        return lon_i(pts).reshape(r.shape), lat_i(pts).reshape(r.shape)

    return SceneRaster(
        scene_id=meta["scene_id"],
        acquired_at=datetime.fromisoformat(meta["acquired_at"]),
        sigma0_vv=cast(NDArray[np.float32], sigma0),
        transform=cast(tuple[float, float, float, float, float, float], tuple(meta["transform"])),
        geolocate=geolocate,
        bytes_downloaded=int(meta.get("bytes_downloaded", 0)),
    )


def delete_raster(store: ObjectStore, scene_id: str) -> None:
    for key in _keys(scene_id):
        store.delete(key)
