"""Build the production `Services` from environment variables.

CDSE_S3_ACCESS_KEY / CDSE_S3_SECRET_KEY   Copernicus S3 keys (the account login is not needed)
STORE_ENDPOINT_URL, STORE_ACCESS_KEY, STORE_SECRET_KEY, STORE_BUCKET (default sar)
PIPELINE_LAND_POLYGONS                     GeoJSON of land polygons covering the Regions
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import cache

from contracts.protocols import LandMask
from pipeline.adapters import (
    CdseCatalogue,
    DmaAisSource,
    S3ObjectStore,
    Sentinel1S3Fetcher,
    create_s3_client,
    create_store_client,
)
from pipeline.algorithms import CaCfarDetector, NearestNeighbourValidator, PolygonLandMask
from pipeline.stages.real import Services


def _need(env: Mapping[str, str], name: str) -> str:
    if not env.get(name):
        raise RuntimeError(f"{name} is not set")
    return env[name]


def build_services(env: Mapping[str, str] | None = None) -> Services:
    e = os.environ if env is None else env
    store = S3ObjectStore(
        create_store_client(
            _need(e, "STORE_ENDPOINT_URL"), _need(e, "STORE_ACCESS_KEY"), _need(e, "STORE_SECRET_KEY")
        ),
        bucket=e.get("STORE_BUCKET", "sar"),
    )
    store.ensure_bucket()
    polygons = _need(e, "PIPELINE_LAND_POLYGONS")

    @cache
    def land_mask_for(near_shore_m: float, exclusion_m: float) -> LandMask:
        return PolygonLandMask.from_geojson(polygons, near_shore_m=near_shore_m, exclusion_m=exclusion_m)

    return Services(
        catalogue=CdseCatalogue(),
        fetcher=Sentinel1S3Fetcher(
            create_s3_client(_need(e, "CDSE_S3_ACCESS_KEY"), _need(e, "CDSE_S3_SECRET_KEY"))
        ),
        ais_source=DmaAisSource(),
        store=store,
        land_mask=lambda cfg: land_mask_for(cfg.land_mask.near_shore_m, cfg.land_mask.exclusion_m),
        detectors={"ca-cfar": CaCfarDetector.from_params},
        validator=NearestNeighbourValidator(),
    )
