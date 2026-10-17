"""The land polygon loader, on a tiny synthetic 'split' shapefile zip (no network, no 0.9 GB download)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest
import shapely
from pyogrio.raw import write  # type: ignore[import-untyped]
from shapely.geometry import Polygon, box

from contracts.protocols import PermanentError, ShoreClass, TransientError
from pipeline.adapters._retry import RetryPolicy
from pipeline.algorithms import PolygonLandMask
from pipeline.landpolygons import ATTRIBUTION, USER_AGENT, build, clip_land, fetch_structures, main

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
    assert build([REGION_YAML], out, zip_path, margin_deg=0.1, structures=False) == 1
    assert json.loads(out.read_text())["attribution"] == ATTRIBUTION

    mask = PolygonLandMask.from_geojson(out, near_shore_m=300)
    assert mask.classify((10.90, 55.30)) is ShoreClass.ON_LAND
    assert mask.classify((11.002, 55.30)) is ShoreClass.NEAR_SHORE  # ~130 m east of the coast
    assert mask.classify((11.10, 55.30)) is ShoreClass.OPEN_WATER


def test_cli_writes_the_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    zip_path = make_zip(tmp_path, [box(10.80, 55.20, 11.00, 55.40)])
    out = tmp_path / "out.geojson"
    main(["--region", str(REGION_YAML), "--out", str(out), "--zip", str(zip_path), "--no-structures"])
    assert "wrote 1 land polygons" in capsys.readouterr().out
    assert json.loads(out.read_text())["features"][0]["geometry"]["type"] == "Polygon"


# ---- structures in the water (Overpass), with the network replaced by a mock transport ----

OVERPASS_REPLY = {
    "elements": [
        {  # a bridge deck, two points ~1.3 km apart
            "type": "way",
            "tags": {"bridge": "yes", "highway": "motorway"},
            "geometry": [{"lat": 55.34, "lon": 11.00}, {"lat": 55.34, "lon": 11.02}],
        },
        {
            "type": "node",
            "tags": {"power": "generator", "generator:source": "wind"},
            "lat": 55.50,
            "lon": 10.90,
        },
        {"type": "node", "tags": {"man_made": "offshore_platform"}, "lat": 55.10, "lon": 11.20},
        {
            "type": "way",
            "tags": {"bridge": "yes", "highway": "path"},
            "geometry": [{"lat": 55.0, "lon": 11.0}],
        },
    ]
}


def overpass_client(replies: list[httpx.Response], seen: list[httpx.Request] | None = None) -> httpx.Client:
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return queue.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler))


NO_WAIT = RetryPolicy(attempts=4, base_delay=0.0, sleep=lambda _: None)


def test_structures_become_buffered_polygons() -> None:
    client = overpass_client([httpx.Response(200, json=OVERPASS_REPLY)])
    polys = fetch_structures(REGION_RING, 0.1, client=client, retry=NO_WAIT)
    assert len(polys) == 3  # bridge, turbine, platform; the one-point way is ignored

    def contains(lon: float, lat: float) -> bool:
        return any(p.contains(shapely.Point(lon, lat)) for p in polys)

    assert contains(11.01, 55.34) and contains(
        11.01, 55.3405
    )  # on the deck and ~55 m beside it (80 m buffer)
    assert not contains(11.01, 55.342)  # ~220 m away
    assert contains(10.90, 55.50) and contains(11.20, 55.10)
    assert not contains(11.00, 55.0)


def test_busy_overpass_is_retried_and_identifies_itself() -> None:
    seen: list[httpx.Request] = []
    busy = httpx.Response(502, text="# 502 Gateway Timeout: server at capacity")
    client = overpass_client([busy, busy, httpx.Response(200, json=OVERPASS_REPLY)], seen)
    client.headers["User-Agent"] = USER_AGENT
    assert len(fetch_structures(REGION_RING, 0.1, client=client, retry=NO_WAIT)) == 3
    assert len(seen) == 3
    assert "bridge" in seen[0].content.decode() and "generator%3Asource" in seen[0].content.decode()


def test_overpass_rejecting_the_query_is_permanent() -> None:
    client = overpass_client([httpx.Response(400, text="parse error")])
    with pytest.raises(PermanentError, match="rejected"):
        fetch_structures(REGION_RING, 0.1, client=client, retry=NO_WAIT)


def test_overpass_down_for_good_is_transient() -> None:
    client = overpass_client([httpx.Response(503)] * 4)
    with pytest.raises(TransientError):
        fetch_structures(REGION_RING, 0.1, client=client, retry=NO_WAIT)


def test_structures_are_masked_like_land(tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path, [box(10.80, 55.20, 11.00, 55.30)])
    out = tmp_path / "land.geojson"
    client = overpass_client([httpx.Response(200, json=OVERPASS_REPLY)])
    # build() takes the client so no network is used
    assert build([REGION_YAML], out, zip_path, margin_deg=0.1, structures=True, client=client) == 4
    mask = PolygonLandMask.from_geojson(out, near_shore_m=300)
    assert mask.classify((11.01, 55.34)) is ShoreClass.ON_LAND  # the bridge deck
    assert mask.classify((11.01, 55.3425)) is ShoreClass.NEAR_SHORE  # ~200 m beyond the buffer
    assert mask.classify((11.10, 55.45)) is ShoreClass.OPEN_WATER
