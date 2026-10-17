"""The land polygon loader, on a tiny synthetic 'split' shapefile zip (no network, no 0.9 GB download)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
import shapely
from pyogrio.raw import write  # type: ignore[import-untyped]
from shapely.geometry import Polygon, box

from contracts.protocols import ShoreClass
from pipeline.algorithms import PolygonLandMask
from pipeline.landpolygons import ATTRIBUTION, build, clip_land, main

REGION_YAML = Path(__file__).resolve().parents[2] / "contracts" / "region.greatbelt.yaml"
# Great Belt Region box is lon 10.70..11.30, lat 55.00..55.70
REGION_RING = [(10.70, 55.00), (11.30, 55.00), (11.30, 55.70), (10.70, 55.70), (10.70, 55.00)]


def make_zip(tmp_path: Path, polygons: list[Polygon]) -> Path:
    """A zip shaped like land-polygons-split-4326.zip: one folder with land_polygons.shp and friends."""
    folder = tmp_path / "land-polygons-split-4326"
    folder.mkdir()
    geometry = np.array([shapely.to_wkb(p) for p in polygons], dtype=object)
    write(
        str(folder / "land_polygons.shp"),
        geometry=geometry,
        field_data=[],
        fields=[],
        geometry_type="Polygon",
        crs="EPSG:4326",
        driver="ESRI Shapefile",
    )
    out = tmp_path / "land-polygons-split-4326.zip"
    with zipfile.ZipFile(out, "w") as zf:
        for f in folder.iterdir():
            zf.write(f, f"{folder.name}/{f.name}")
    return out


def test_tile_seams_are_dissolved(tmp_path: Path) -> None:
    left, right = box(10.80, 55.20, 11.00, 55.40), box(11.00, 55.20, 11.20, 55.40)  # one island, two tiles
    zip_path = make_zip(tmp_path, [left, right])
    (island,) = clip_land(zip_path, REGION_RING, margin_deg=0.1)
    assert island.equals(left.union(right))


def test_land_outside_region_and_margin_is_dropped(tmp_path: Path) -> None:
    inside, near_margin, far = (
        box(10.9, 55.3, 11.0, 55.4),
        box(11.32, 55.3, 11.35, 55.4),
        box(14.0, 55.0, 14.5, 55.5),
    )
    zip_path = make_zip(tmp_path, [inside, near_margin, far])
    parts = clip_land(zip_path, REGION_RING, margin_deg=0.1)
    assert len(parts) == 2  # `far` is gone
    assert max(p.bounds[2] for p in parts) <= 11.30 + 0.1 + 1e-9


def test_land_is_clipped_to_region_plus_margin(tmp_path: Path) -> None:
    big = box(9.0, 54.0, 12.5, 56.5)  # swallows the whole Region
    zip_path = make_zip(tmp_path, [big])
    (clipped,) = clip_land(zip_path, REGION_RING, margin_deg=0.1)
    assert clipped.bounds == pytest.approx((10.60, 54.90, 11.40, 55.80))


def test_no_land_gives_no_polygons(tmp_path: Path) -> None:
    assert clip_land(make_zip(tmp_path, [box(14.0, 55.0, 14.5, 55.5)]), REGION_RING, 0.1) == []


def test_output_feeds_the_land_mask_and_keeps_attribution(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path, [box(10.80, 55.20, 11.00, 55.40)])
    out = tmp_path / "land" / "great-belt.geojson"
    assert build([REGION_YAML], out, zip_path, margin_deg=0.1) == 1
    assert json.loads(out.read_text())["attribution"] == ATTRIBUTION

    mask = PolygonLandMask.from_geojson(out, near_shore_m=300)
    assert mask.classify((10.90, 55.30)) is ShoreClass.ON_LAND
    assert mask.classify((11.002, 55.30)) is ShoreClass.NEAR_SHORE  # ~130 m east of the coast
    assert mask.classify((11.10, 55.30)) is ShoreClass.OPEN_WATER


def test_cli_writes_the_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    zip_path = make_zip(tmp_path, [box(10.80, 55.20, 11.00, 55.40)])
    out = tmp_path / "out.geojson"
    main(["--region", str(REGION_YAML), "--out", str(out), "--zip", str(zip_path)])
    assert "wrote 1 land polygons" in capsys.readouterr().out
    assert json.loads(out.read_text())["features"][0]["geometry"]["type"] == "Polygon"
