"""Regression on the real Great Belt land (built by `make land`; skipped when the file is absent).

Runs the committed Sentinel-1 fixture through fetch + CFAR with real OSM land: the three known ships must be
the only open-water Detections, and every other Detection must be flagged near-shore, not lost or on land.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path

import boto3
import numpy as np
import pytest
import yaml
from moto import mock_aws

from contracts.protocols import Region, SceneRef, ShoreClass
from pipeline.adapters import Sentinel1S3Fetcher
from pipeline.algorithms import CaCfarDetector, PolygonLandMask

ROOT = Path(__file__).resolve().parents[2]
LAND = ROOT / "data" / "land" / "great-belt.geojson"
FIX = ROOT / "tests" / "fixtures"

pytestmark = pytest.mark.skipif(not LAND.exists(), reason="run `make land` to build the real land polygons")


def test_only_the_known_ships_are_open_water_detections() -> None:
    meta = json.loads((FIX / "scene" / "meta.json").read_text())
    expected = json.loads((FIX / "scene" / "expected_ships.json").read_text())
    cfg = yaml.safe_load((ROOT / "contracts" / "region.greatbelt.yaml").read_text())
    region = Region("great-belt", "Great Belt", [tuple(p) for p in cfg["polygon"]])
    ref = SceneRef(
        meta["scene_id"], datetime.fromisoformat(meta["acquired_at"]), [tuple(p) for p in meta["footprint"]]
    )

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
        s3.create_bucket(Bucket="eodata")
        root = FIX / "s3" / "eodata"
        for path in root.rglob("*"):
            if path.is_file():
                s3.put_object(Bucket="eodata", Key=path.relative_to(root).as_posix(), Body=path.read_bytes())
        fetcher = Sentinel1S3Fetcher(s3)
        raster = fetcher.fetch(ref, region)
    assert fetcher.last_diagnostics is not None and raster.geolocate is not None
    r0, _, c0, _ = fetcher.last_diagnostics.window

    land = PolygonLandMask.from_geojson(LAND, near_shore_m=300)
    detector = CaCfarDetector.from_params(cfg["detector"]["params"])
    found = detector.detect(dataclasses.replace(raster, water_mask=land.not_land_mask(raster)))
    shore = [land.classify(d.position) for d in found]

    assert ShoreClass.ON_LAND not in shore
    open_water = [d for d, s in zip(found, shore, strict=True) if s is ShoreClass.OPEN_WATER]
    assert len(open_water) == len(expected["ships"])
    for ship in expected["ships"]:
        lon, lat = raster.geolocate(np.array([ship["row"] - r0], float), np.array([ship["col"] - c0], float))
        nearest = min(
            np.hypot((d.position[0] - lon[0]) * 62_000, (d.position[1] - lat[0]) * 111_000)
            for d in open_water
        )
        assert nearest < 60, f"ship {ship['mmsi']}: nearest open-water detection {nearest:.0f} m away"
