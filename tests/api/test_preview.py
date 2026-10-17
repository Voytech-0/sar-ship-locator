"""Scene preview: lon/lat PNG from a raster with a rotated geolocation."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import numpy as np
from PIL import Image

from contracts.protocols import SceneRaster
from pipeline.stages.preview import make_preview


def _raster(sigma0: np.ndarray) -> SceneRaster:
    def geolocate(rows: np.ndarray, cols: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        # about 10 m pixels, grid rotated 12 degrees from north
        t = np.radians(12)
        x, y = cols * 10.0, rows * 10.0
        east, north = x * np.cos(t) + y * np.sin(t), -x * np.sin(t) + y * np.cos(t)
        lat = 55.0 - north / 111_320.0
        return 11.0 + east / (111_320.0 * np.cos(np.radians(55.0))), lat

    return SceneRaster(
        "S1_A",
        datetime(2026, 10, 14, tzinfo=UTC),
        sigma0.astype(np.float32),
        (0, 0, 0, 0, 0, 0),
        geolocate=geolocate,
    )


def test_preview_is_georeferenced_and_has_no_holes() -> None:
    sigma0 = np.full((600, 600), 0.05, dtype=np.float32)
    sigma0[:, :30] = np.nan  # outside the Region
    preview = make_preview(_raster(sigma0))
    assert preview is not None
    img = np.asarray(Image.open(io.BytesIO(preview.png)))
    south, west, north, east = preview.bounds
    assert img.shape[2] == 2 and north > south and east > west
    assert 5.5e3 < (north - south) * 111_320 < 8.5e3  # 6 km of raster, rotated
    alpha = img[..., 1] > 0
    inner = alpha[10:-10, 10:-10]
    assert (img[..., 0][alpha] == img[..., 0][alpha][0]).all()  # constant input stays constant
    assert 0.5 < inner.mean() < 1.0  # the rotated frame leaves transparent corners, not holes
    # no transparent cell is enclosed by opaque neighbours on both sides in a row
    row = alpha[alpha.shape[0] // 2]
    xs = np.flatnonzero(row)
    assert row[xs[0] : xs[-1] + 1].all()


def test_all_nan_raster_has_no_preview() -> None:
    assert make_preview(_raster(np.full((30, 30), np.nan))) is None
