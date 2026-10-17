"""Smoke tests against the real CDSE and DMA services. Skipped unless RUN_LIVE=1.

    RUN_LIVE=1 SAR_ENV_FILE=/path/to/.env uv run pytest tests/adapters/test_live.py -s

S3 credentials come from CDSE_S3_ACCESS_KEY / CDSE_S3_SECRET_KEY in the environment, or from the file named
by SAR_ENV_FILE. The DMA day download (about 700 MB) additionally needs RUN_LIVE_AIS=1.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import yaml

from contracts.protocols import Region
from pipeline.adapters import CdseCatalogue, DmaAisSource, Sentinel1S3Fetcher, create_s3_client

live = pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to hit real services")
live_ais = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_AIS") != "1", reason="set RUN_LIVE_AIS=1 (700 MB download)"
)

KNOWN_SCENE = "S1D_IW_GRDH_1SDV_20261014T053204_20261014T053229_005007_009759_0CC6"
REGION_FILE = Path(__file__).resolve().parents[2] / "contracts" / "region.greatbelt.yaml"


def great_belt() -> Region:
    cfg = yaml.safe_load(REGION_FILE.read_text())
    return Region(cfg["region_id"], cfg["name"], [tuple(p) for p in cfg["polygon"]])


def env_value(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    env_file = os.environ.get("SAR_ENV_FILE")
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip() or None
    return None


@live
def test_live_catalogue_lists_recent_great_belt_scenes() -> None:
    scenes = CdseCatalogue().search(great_belt(), datetime.now(UTC) - timedelta(days=14))
    print(f"\ncatalogue: {len(scenes)} scenes in 14 days, newest {scenes[0].scene_id if scenes else None}")
    assert scenes
    assert all(s.footprint[0] == s.footprint[-1] and s.acquired_at.tzinfo for s in scenes)


@live
def test_live_fetch_known_scene() -> None:
    key, secret = env_value("CDSE_S3_ACCESS_KEY"), env_value("CDSE_S3_SECRET_KEY")
    if not (key and secret):
        pytest.skip("no CDSE S3 credentials")
    region = great_belt()
    scenes = CdseCatalogue().search(region, datetime(2026, 10, 13, tzinfo=UTC))
    scene = next((s for s in scenes if s.scene_id == KNOWN_SCENE), None)
    if scene is None:
        pytest.skip(f"{KNOWN_SCENE} no longer in the catalogue window")

    fetcher = Sentinel1S3Fetcher(create_s3_client(key, secret))
    raster = fetcher.fetch(scene, region)
    diag = fetcher.last_diagnostics
    assert diag is not None

    sigma0 = raster.sigma0_vv
    valid = sigma0[np.isfinite(sigma0) & (sigma0 > 0)]
    db = 10 * np.log10(valid)
    p20, p50 = np.percentile(db, [20, 50])
    print(
        f"\nmode={diag.mode} window={diag.window} shape={sigma0.shape} "
        f"downloaded={diag.bytes_downloaded / 1e6:.0f} MB in {diag.requests} requests, {diag.seconds:.0f} s"
        f"\nsigma0 VV dB: p20={p20:.1f} median={p50:.1f} finite={np.isfinite(sigma0).mean():.2f}"
        f"\naffine vs GCP bilinear: max {diag.affine_max_error_px:.1f} px "
        f"({diag.affine_max_error_m:.0f} m), mean {diag.affine_mean_error_px:.1f} px"
    )
    assert diag.mode == "row-range"
    assert diag.bytes_downloaded < 0.6 * 883e6
    assert sigma0.shape == (diag.window[1] - diag.window[0], diag.window[3] - diag.window[2])
    assert np.isfinite(sigma0).mean() > 0.3
    assert (
        -32 < p20 < -12
    )  # open water dominates the dark end of the histogram (VV sea clutter ~ -20 to -25 dB)


@live
def test_live_dma_latest_available_day() -> None:
    day = DmaAisSource().latest_available_day()
    print(f"\nlatest DMA day: {day}")
    assert day is not None
    assert 1 <= (datetime.now(UTC).date() - day).days <= 10


@live
@live_ais
def test_live_dma_positions_for_latest_day() -> None:
    source = DmaAisSource()
    day = source.latest_available_day()
    assert day is not None
    region = great_belt()
    positions = list(source.positions(day, region))
    print(
        f"\nDMA {day}: {len(positions)} positions in {region.name}, "
        f"{len({p.mmsi for p in positions})} vessels"
    )
    assert len(positions) > 100
    assert all(10.7 <= p.position[0] <= 11.3 and 55.0 <= p.position[1] <= 55.7 for p in positions)
    assert all(p.ts.date() == day for p in positions)
