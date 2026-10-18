import dataclasses
import time
from pathlib import Path

import numpy as np
import pytest
import yaml

from pipeline.algorithms import CaCfarDetector, TooManyHitsError
from tests.algorithms.synthetic import (
    TRANSFORM,
    Ship,
    add_ships,
    make_raster,
    pixel_to_lonlat,
    speckled_sea,
)

REGION_CONFIG = Path(__file__).resolve().parents[2] / "contracts" / "region.greatbelt.yaml"

SHIPS = [
    Ship(100, 100, 4, 4),
    Ship(150, 600, 3, 10),  # elongated, ~100 m x 30 m
    Ship(400, 200, 5, 5, gain=30.0),  # fainter, 15 dB
    Ship(600, 650, 12, 4),
    Ship(700, 100, 3, 3, gain=60.0),
    Ship(300, 400, 8, 8),
    Ship(50, 450, 6, 3),
    Ship(500, 500, 4, 12),
]


@pytest.fixture(scope="module")
def detector() -> CaCfarDetector:
    cfg = yaml.safe_load(REGION_CONFIG.read_text())["detector"]
    return CaCfarDetector.from_params({**cfg["params"], "version": cfg["version"]})


def test_identity_and_params_from_region_config(detector: CaCfarDetector) -> None:
    cfg = yaml.safe_load(REGION_CONFIG.read_text())["detector"]
    assert (detector.name, detector.version) == (cfg["name"], cfg["version"])
    assert detector.guard_cells == 8 and detector.background_cells == 24
    assert detector.threshold > 2  # a threshold below twice the background would flood with false alarms


def test_rejects_unknown_params() -> None:
    with pytest.raises(ValueError, match="unknown"):
        CaCfarDetector.from_params({"guard_cells": 3, "nonsense": 1})


def test_finds_every_injected_ship_with_no_false_alarms(detector: CaCfarDetector) -> None:
    img = speckled_sea((800, 800), seed=1)
    add_ships(img, SHIPS)
    found = detector.detect(make_raster(img))

    assert len(found) == len(SHIPS), [d.pixel_box for d in found]
    for ship in SHIPS:
        lon, lat = pixel_to_lonlat(*ship.center)
        nearest = min(found, key=lambda d: abs(d.position[0] - lon) + abs(d.position[1] - lat))
        # Within 2 pixels (about 20 m) of the true centre.
        assert abs(nearest.position[0] - lon) < 2 * abs(TRANSFORM[0])
        assert abs(nearest.position[1] - lat) < 2 * abs(TRANSFORM[4])


def test_positions_come_from_geolocate_when_set(detector: CaCfarDetector) -> None:
    img = speckled_sea((200, 200), seed=2)
    add_ships(img, [Ship(100, 100, 4, 4)])
    plain = detector.detect(make_raster(img))
    assert len(plain) == 1

    def shifted(rows: np.ndarray, cols: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return cols * 0.0 + 12.5, rows * 0.0 + 55.5

    raster = dataclasses.replace(make_raster(img), geolocate=shifted)
    (found,) = detector.detect(raster)
    assert found.position == (12.5, 55.5)
    assert found.pixel_box == plain[0].pixel_box


def test_candidate_attributes(detector: CaCfarDetector) -> None:
    img = speckled_sea((400, 400), seed=2)
    ship = Ship(200, 150, 4, 24)  # 240 m long along columns (east-west)
    add_ships(img, [ship])
    (det,) = detector.detect(make_raster(img))

    assert 0.0 < det.confidence <= 1.0
    r0, c0, r1, c1 = det.pixel_box
    assert r0 >= 200 and r1 <= 204 and c0 >= 150 and c1 <= 174  # box lies inside the ship
    assert r1 > r0 and c1 > c0
    # Pixel is ~8.8 m x 10 m; 24 columns is ~210 m. Hits cover most of it, so allow a wide band.
    assert det.length_m is not None
    assert 120 < det.length_m < 260


def test_length_bias_is_subtracted_but_never_below_one_pixel() -> None:
    img = speckled_sea((400, 400), seed=2)
    add_ships(img, [Ship(200, 150, 4, 24)])
    (plain,) = CaCfarDetector().detect(make_raster(img))
    (biased,) = CaCfarDetector(length_bias_m=20).detect(make_raster(img))
    assert plain.length_m is not None and biased.length_m is not None
    assert biased.length_m == pytest.approx(plain.length_m - 20, abs=1e-6)
    (tiny,) = CaCfarDetector(length_bias_m=10_000).detect(make_raster(img))
    assert tiny.length_m is not None and 5 < tiny.length_m < 15  # one pixel
    with pytest.raises(ValueError, match="length_bias_m"):
        CaCfarDetector(length_bias_m=-1)


def test_brighter_ship_has_higher_confidence(detector: CaCfarDetector) -> None:
    img = speckled_sea((300, 300), seed=3)
    add_ships(img, [Ship(100, 50, 5, 5, gain=25.0), Ship(100, 200, 5, 5, gain=400.0)])
    dim, bright = sorted(detector.detect(make_raster(img)), key=lambda d: d.pixel_box[1])
    assert bright.confidence > dim.confidence


def test_clean_sea_has_no_detections(detector: CaCfarDetector) -> None:
    img = speckled_sea((1000, 1000), seed=4)
    assert detector.detect(make_raster(img)) == []


def test_land_is_excluded_by_water_mask() -> None:
    # A loose detector on purpose: this tests the mask, and a strict one finds no land clutter to exclude.
    detector = CaCfarDetector(guard_cells=8, background_cells=24, pfa=1e-6, min_cluster_pixels=3)
    rows, cols = 600, 800
    img = speckled_sea((rows, cols), seed=5)
    land = np.zeros((rows, cols), dtype=bool)
    land[:, :300] = True
    # Bright, heterogeneous land clutter, as in coastal farmland and towns.
    rng = np.random.default_rng(6)
    img[:, :300] = rng.gamma(shape=1.0, scale=0.2, size=(rows, 300)).astype(np.float32)
    ship = Ship(300, 600, 5, 5)
    add_ships(img, [ship])

    unmasked = detector.detect(make_raster(img))
    masked = detector.detect(make_raster(img, water_mask=~land))

    assert any(d.pixel_box[1] < 300 for d in unmasked)  # without the mask, land gives false alarms
    assert [d.pixel_box[1] for d in masked] and all(d.pixel_box[1] >= 600 for d in masked)
    assert len(masked) == 1


def test_sea_next_to_masked_land_is_not_biased(detector: CaCfarDetector) -> None:
    """Background at the coast must use sea pixels only: a ship 12 px off the coast is still found."""
    rows, cols = 300, 400
    img = speckled_sea((rows, cols), seed=7)
    land = np.zeros((rows, cols), dtype=bool)
    land[:, :200] = True
    img[:, :200] = 0.5
    add_ships(img, [Ship(150, 212, 4, 4)])
    found = detector.detect(make_raster(img, water_mask=~land))
    assert len(found) == 1


def test_nan_regions_are_ignored(detector: CaCfarDetector) -> None:
    img = speckled_sea((400, 400), seed=8)
    img[:, :150] = np.nan
    add_ships(img, [Ship(200, 300, 4, 4)])
    found = detector.detect(make_raster(img))
    assert len(found) == 1


def test_all_invalid_raster_returns_nothing(detector: CaCfarDetector) -> None:
    img = np.full((200, 200), np.nan, dtype=np.float32)
    assert detector.detect(make_raster(img)) == []
    img2 = speckled_sea((200, 200), seed=9)
    assert detector.detect(make_raster(img2, water_mask=np.zeros((200, 200), dtype=bool))) == []


def test_too_many_hits_guard() -> None:
    img = speckled_sea((300, 300), seed=10)
    add_ships(img, [Ship(100, 100, 30, 30)])
    with pytest.raises(TooManyHitsError):
        CaCfarDetector(max_hits=10).detect(make_raster(img))


def test_chunking_does_not_change_results() -> None:
    img = speckled_sea((700, 500), seed=11)
    add_ships(img, [Ship(255, 100, 6, 6), Ship(512, 300, 4, 8), Ship(20, 400, 5, 5)])
    raster = make_raster(img)
    whole = CaCfarDetector(chunk_rows=4096).detect(raster)
    chunked = CaCfarDetector(chunk_rows=64).detect(raster)
    assert [d.pixel_box for d in whole] == [d.pixel_box for d in chunked]
    assert len(whole) == 3


def test_runtime_on_full_scene_size(detector: CaCfarDetector) -> None:
    """Region-sized raster (5000 x 7500) must run in seconds; the bound is deliberately generous."""
    img = speckled_sea((5000, 7500), seed=12)
    add_ships(img, [Ship(1000, 1000, 6, 6), Ship(4000, 6000, 5, 9)])
    raster = make_raster(img)
    start = time.perf_counter()
    found = detector.detect(raster)
    elapsed = time.perf_counter() - start
    assert len(found) == 2
    assert elapsed < 90, f"CFAR took {elapsed:.1f} s"
