"""Synthetic SAR rasters for the algorithm tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
from numpy.typing import NDArray

from contracts.protocols import SceneRaster
from pipeline.algorithms.geo import Affine6

ENL = 4.4
SEA_MEAN = 0.01  # sigma0 linear, about -20 dB
# 10 m pixels near 55.4 N, north-up: lon step 1.58e-4 deg, lat step 0.9e-4 deg.
TRANSFORM: Affine6 = (1.58e-4, 0.0, 10.70, 0.0, -0.9e-4, 55.70)
ACQUIRED = datetime(2026, 10, 14, 5, 32, 20, tzinfo=UTC)


@dataclass(frozen=True)
class Ship:
    row: int  # top-left pixel
    col: int
    height: int
    width: int
    gain: float = 100.0  # mean intensity relative to sea (20 dB)

    @property
    def center(self) -> tuple[float, float]:
        return self.row + self.height / 2, self.col + self.width / 2


def speckled_sea(
    shape: tuple[int, int], seed: int, mean: float = SEA_MEAN, enl: float = ENL
) -> NDArray[np.float32]:
    rng = np.random.default_rng(seed)
    return rng.gamma(shape=enl, scale=mean / enl, size=shape).astype(np.float32)


def add_ships(img: NDArray[np.float32], ships: list[Ship], seed: int = 99) -> None:
    rng = np.random.default_rng(seed)
    for s in ships:
        block = rng.gamma(shape=ENL, scale=SEA_MEAN * s.gain / ENL, size=(s.height, s.width))
        img[s.row : s.row + s.height, s.col : s.col + s.width] = block.astype(np.float32)


def make_raster(img: NDArray[np.float32], water_mask: NDArray[np.bool_] | None = None) -> SceneRaster:
    return SceneRaster(
        scene_id="S1X_TEST",
        acquired_at=ACQUIRED,
        sigma0_vv=img,
        transform=TRANSFORM,
        water_mask=water_mask,
    )


def pixel_to_lonlat(row: float, col: float) -> tuple[float, float]:
    a, b, c, d, e, f = TRANSFORM
    return a * col + b * row + c, d * col + e * row + f
