import json
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Polygon, box

from contracts.protocols import ShoreClass
from pipeline.algorithms import CaCfarDetector, PolygonLandMask
from pipeline.algorithms.landmask import rasterize
from tests.algorithms.synthetic import (
    TRANSFORM,
    Ship,
    add_ships,
    make_raster,
    pixel_to_lonlat,
    speckled_sea,
)

SHAPE = (400, 600)


def _lonlat_box(row0: int, col0: int, row1: int, col1: int) -> Polygon:
    (x0, y0), (x1, y1) = pixel_to_lonlat(row0, col0), pixel_to_lonlat(row1, col1)
    return box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def test_rasterize_matches_shapely_with_hole() -> None:
    outer = [pixel_to_lonlat(r, c) for r, c in [(50, 40), (50, 300), (320, 350), (300, 60), (50, 40)]]
    hole = [pixel_to_lonlat(r, c) for r, c in [(120, 120), (120, 200), (200, 200), (200, 120), (120, 120)]]
    geom = Polygon(outer, [hole])
    got = rasterize(geom, TRANSFORM, SHAPE)

    rows, cols = np.mgrid[0 : SHAPE[0], 0 : SHAPE[1]]
    a, _, c, _, e, f = TRANSFORM
    lons, lats = a * (cols + 0.5) + c, e * (rows + 0.5) + f
    expected = shapely.contains_xy(geom, lons, lats)
    assert (got != expected).sum() <= 4  # boundary pixels may differ by floating point only
    assert not got[150, 150]  # inside the hole
    assert got[100, 100]


def test_rasterize_empty_and_outside() -> None:
    assert not rasterize(Polygon(), TRANSFORM, SHAPE).any()
    far = box(20, 20, 21, 21)
    assert not rasterize(far, TRANSFORM, SHAPE).any()


def test_rasterize_polygon_larger_than_raster() -> None:
    huge = box(0, 0, 30, 80)
    assert rasterize(huge, TRANSFORM, SHAPE).all()


def test_water_mask_excludes_land_and_near_shore_band() -> None:
    land = _lonlat_box(0, 0, 400, 200)  # west third of the raster
    mask = PolygonLandMask(land, near_shore_m=300.0)
    raster = make_raster(speckled_sea(SHAPE, seed=1))
    water = mask.water_mask(raster)
    not_land = mask.not_land_mask(raster)

    assert not water[:, :150].any()  # land
    assert not not_land[:, :190].any() and not_land[:, 215:].all()
    # 300 m is about 30-34 px: band covers cols ~200-234, then open water.
    assert not water[200, 210] and not water[200, 225]
    assert water[200, 250] and water[200, 500]
    assert not_land[200, 210]  # near-shore band is kept by not_land_mask


def test_no_land_in_view_gives_all_water() -> None:
    mask = PolygonLandMask(box(20, 20, 21, 21))
    raster = make_raster(speckled_sea(SHAPE, seed=2))
    assert mask.water_mask(raster).all()
    assert PolygonLandMask([]).water_mask(raster).all()


def test_classify_on_land_near_shore_and_open_water() -> None:
    land = box(10.70, 55.60, 10.80, 55.70)  # block in the north-west corner
    mask = PolygonLandMask(land, near_shore_m=300.0)
    assert mask.classify((10.75, 55.65)) == ShoreClass.ON_LAND
    # 150 m south of the coast: 150 / 111_200 deg.
    assert mask.classify((10.75, 55.60 - 150 / 111_200)) == ShoreClass.NEAR_SHORE
    assert mask.classify((10.75, 55.60 - 600 / 111_200)) == ShoreClass.OPEN_WATER
    assert mask.classify((11.20, 55.30)) == ShoreClass.OPEN_WATER


def test_classify_near_shore_distance_uses_metres_east_west() -> None:
    land = box(10.70, 55.60, 10.80, 55.70)
    mask = PolygonLandMask(land, near_shore_m=300.0)
    # At 55.65 N a degree of longitude is ~63.4 km: 200 m east of the coast is near shore, 500 m is not.
    assert mask.classify((10.80 + 200 / 63_400, 55.65)) == ShoreClass.NEAR_SHORE
    assert mask.classify((10.80 + 500 / 63_400, 55.65)) == ShoreClass.OPEN_WATER


def test_classify_with_no_land() -> None:
    assert PolygonLandMask([]).classify((10.0, 55.0)) == ShoreClass.OPEN_WATER


def test_from_geojson(tmp_path: Path) -> None:
    land = box(10.70, 55.60, 10.80, 55.70)
    path = tmp_path / "land.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {}, "geometry": shapely.geometry.mapping(land)}
                ],
            }
        )
    )
    mask = PolygonLandMask.from_geojson(path)
    assert mask.classify((10.75, 55.65)) == ShoreClass.ON_LAND


def test_near_shore_ship_found_and_flagged_but_not_with_water_mask() -> None:
    """The intended wiring: detect on not_land_mask, flag with classify; water_mask would hide the ship."""
    land = _lonlat_box(0, 0, 400, 200)
    mask = PolygonLandMask(land, near_shore_m=300.0)
    img = speckled_sea(SHAPE, seed=3)
    img[:, :200] = 0.3  # bright land
    ship = Ship(200, 220, 4, 4)  # ~20 px = 180 m off the coast
    add_ships(img, [ship])
    raster = make_raster(img)
    detector = CaCfarDetector()

    via_not_land = detector.detect(make_raster(img, water_mask=mask.not_land_mask(raster)))
    via_water = detector.detect(make_raster(img, water_mask=mask.water_mask(raster)))

    assert len(via_not_land) == 1
    assert mask.classify(via_not_land[0].position) == ShoreClass.NEAR_SHORE
    assert via_water == []


def test_exclusion_zone_is_ignored_like_land_but_the_band_beyond_is_only_flagged() -> None:
    land = box(10.70, 55.60, 10.80, 55.70)
    mask = PolygonLandMask(land, near_shore_m=300.0, exclusion_m=100.0)

    def south(metres: float) -> tuple[float, float]:
        return 10.75, 55.60 - metres / 111_200

    assert mask.classify(south(60)) == ShoreClass.ON_LAND  # inside the 100 m zone
    assert mask.classify(south(150)) == ShoreClass.NEAR_SHORE
    assert mask.classify(south(600)) == ShoreClass.OPEN_WATER
    # without the zone the same 60 m point is just near-shore
    assert PolygonLandMask(land, near_shore_m=300.0).classify(south(60)) == ShoreClass.NEAR_SHORE


def test_exclusion_zone_is_removed_from_the_detection_mask_in_metres() -> None:
    land = _lonlat_box(0, 0, 400, 200)
    raster = make_raster(speckled_sea(SHAPE, seed=1))
    plain = PolygonLandMask(land, near_shore_m=300.0).not_land_mask(raster)
    zoned = PolygonLandMask(land, near_shore_m=300.0, exclusion_m=100.0).not_land_mask(raster)
    # about 10 m per pixel: the zone removes roughly ten more columns beside the coast, nothing elsewhere
    removed = int((plain & ~zoned)[200].sum())
    assert 8 <= removed <= 13
    assert not (zoned & ~plain).any()
    assert zoned[200, 240] and zoned[200, 500]


def test_an_exclusion_wider_than_the_band_widens_the_band() -> None:
    land = box(10.70, 55.60, 10.80, 55.70)
    mask = PolygonLandMask(land, near_shore_m=50.0, exclusion_m=100.0)
    assert mask.near_shore_m == 100.0
    assert mask.classify((10.75, 55.60 - 150 / 111_200)) == ShoreClass.OPEN_WATER
