"""The five real stage handlers end to end, over the committed fixtures (real Sentinel-1 crop, AIS slice).

S3 is moto (the Copernicus bucket seeded from tests/fixtures/s3, plus the app's own `sar` bucket), the
catalogue and AIS source are fakes reading the fixtures, the database is a real PostGIS container.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import boto3
import numpy as np
import pytest
from moto import mock_aws
from shapely.geometry import box

from contracts.protocols import AisPosition, Region, SceneRef
from pipeline.adapters import S3ObjectStore, Sentinel1S3Fetcher
from pipeline.algorithms import CaCfarDetector, NearestNeighbourValidator, PolygonLandMask
from pipeline.algorithms.ais_dma import iter_dma_positions
from pipeline.stages.real import Services, make_registry
from tests.api.conftest import Api

FIX = Path(__file__).resolve().parents[1] / "fixtures"
META = json.loads((FIX / "scene" / "meta.json").read_text())
EXPECTED = json.loads((FIX / "scene" / "expected_ships.json").read_text())
AIS_CSV = next((FIX / "ais").glob("*.csv"))
SCENE_ID = META["scene_id"]
ACQUIRED = datetime.fromisoformat(META["acquired_at"])
REF = SceneRef(SCENE_ID, ACQUIRED, [tuple(p) for p in META["footprint"]])


class FakeCatalogue:
    def search(self, region: Region, since: datetime) -> list[SceneRef]:
        return [REF]


class FixtureAis:
    def latest_available_day(self) -> date | None:
        return date(2026, 10, 14)

    def positions(self, day: date, region: Region) -> Iterator[AisPosition]:
        return iter_dma_positions(AIS_CSV, region)


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
        client.create_bucket(Bucket="eodata")
        client.create_bucket(Bucket="sar")
        root = FIX / "s3" / "eodata"
        for path in root.rglob("*"):
            if path.is_file():
                client.put_object(
                    Bucket="eodata", Key=path.relative_to(root).as_posix(), Body=path.read_bytes()
                )
        yield client


@pytest.fixture
def pipeline(s3: Any, make_api: Callable[..., Api]) -> tuple[Api, S3ObjectStore, dict[str, Any]]:
    fetcher = Sentinel1S3Fetcher(s3)
    region = Region(
        "great-belt",
        "Great Belt",
        [(10.70, 55.00), (11.30, 55.00), (11.30, 55.70), (11.30 - 0.6, 55.70), (10.70, 55.00)],
    )
    # Where the known false alarm (a bright spot on the island) is, from the real GCP geolocation.
    probe = fetcher.fetch(REF, region)
    r0, _, c0, _ = fetcher.last_diagnostics.window  # type: ignore[union-attr]
    assert probe.geolocate is not None
    fa = EXPECTED["known_false_alarms"][0]
    lon, lat = probe.geolocate(
        np.array([fa["row"] - r0], dtype=np.float64), np.array([fa["col"] - c0], dtype=np.float64)
    )
    island = box(lon[0] - 0.004, lat[0] - 0.003, lon[0] + 0.004, lat[0] + 0.003)

    store = S3ObjectStore(s3, bucket="sar")
    services = Services(
        catalogue=FakeCatalogue(),
        fetcher=Sentinel1S3Fetcher(s3),
        ais_source=FixtureAis(),
        store=store,
        land_mask=lambda cfg: PolygonLandMask(island, near_shore_m=cfg.land_mask.near_shore_m),
        detectors={"ca-cfar": CaCfarDetector.from_params},
        validator=NearestNeighbourValidator(),
    )
    api = make_api(registry=make_registry(services))
    return api, store, {"island": (lon[0], lat[0]), "r0": r0, "c0": c0, "geolocate": probe.geolocate}


def test_fixture_scene_through_all_five_stages(
    pipeline: tuple[Api, S3ObjectStore, dict[str, Any]],
) -> None:
    api, store, probe = pipeline

    assert api.discover() == [SCENE_ID]
    assert api.scene(SCENE_ID)["status"] == "discovered"

    fetch = api.run("fetch", {"scene_id": SCENE_ID})
    assert fetch["status"] == "succeeded", fetch
    assert fetch["result"]["bytes_downloaded"] > 0
    assert store.exists(f"scenes/{SCENE_ID}/sigma0.npy")
    stored = api.scene(SCENE_ID)
    assert stored["status"] == "fetched"
    # acquired_at is now the time of the Region data's centre row, a little after the crop's first line
    assert datetime.fromisoformat(stored["acquired_at"]) != ACQUIRED.astimezone(UTC)

    detect = api.run("detect", {"scene_id": SCENE_ID})
    assert detect["status"] == "succeeded", detect
    result = detect["result"]
    print("detect result:", result)
    assert result["detector_name"] == "ca-cfar"
    # land is masked before detection, so the island spot is never found
    assert not store.exists(f"scenes/{SCENE_ID}/sigma0.npy"), "raster is deleted after detect (retention)"

    dets = api.client.get(f"/v1/scenes/{SCENE_ID}/detections").json()["features"]
    assert len(dets) == result["detection_count"] >= 3
    for d in dets:
        assert d["properties"]["chip_key"] and store.exists(d["properties"]["chip_key"])
        assert d["properties"]["shore_class"] in ("open_water", "near_shore")
        assert (
            abs(d["geometry"]["coordinates"][0] - probe["island"][0]) > 0.004
            or abs(d["geometry"]["coordinates"][1] - probe["island"][1]) > 0.003
        )
    chip = np.load(io.BytesIO(store.get(dets[0]["properties"]["chip_key"].replace(".png", ".npy"))))
    assert chip.shape == (64, 64) and chip.dtype == np.float32

    load = api.run("ais-load", {"region_id": "great-belt", "date": "2026-10-14"})
    assert load["status"] == "succeeded", load
    assert load["result"]["positions_loaded"] > 1000
    again = api.run("ais-load", {"region_id": "great-belt", "date": "2026-10-14"})
    assert again["status"] == "succeeded"  # same Job (200); idempotent

    validate = api.run("validate", {"scene_id": SCENE_ID})
    assert validate["status"] == "succeeded", validate
    print("validate result:", validate["result"])
    dets = api.client.get(f"/v1/scenes/{SCENE_ID}/detections").json()["features"]
    matched = {d["properties"]["matched_mmsi"] for d in dets if d["properties"]["label"] == "matched"}
    assert {s["mmsi"] for s in EXPECTED["ships"]} <= matched
    assert api.scene(SCENE_ID)["status"] == "validated"

    body = api.client.get("/v1/metrics").text
    assert 'sar_scenes{region_id="great-belt",status="validated"} 1.0' in body
    assert 'sar_jobs_total{stage="detect",status="succeeded"}' in body
    assert "sar_detections_total{" in body and "sar_validations_total{" in body
    assert 'sar_ais_positions_loaded_total{region_id="great-belt"}' in body
    assert 'sar_bytes_downloaded_total{source="cdse"}' in body
    assert 'sar_source_requests_total{outcome="ok",source="dma"}' in body
