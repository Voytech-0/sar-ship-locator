"""Stub stage handlers: canned, schema-valid results plus the database rows a real stage would write.

They let the API, the DAGs and the integration tests run end to end before the real stages exist.
They are deterministic, and idempotent like real handlers must be. Nothing here touches the network
or the object store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pipeline import db
from pipeline.api.models import (
    AisLoadRequest,
    AisLoadResult,
    DetectRequest,
    DetectResult,
    DiscoverRequest,
    DiscoverResult,
    FetchResult,
    SceneStageRequest,
    ValidateResult,
)
from pipeline.stages import StageContext, region_for_scene

STUB_SCENES_PER_REGION = 2
STUB_MMSI = 219000001


def stub_scene_id(region_id: str, n: int) -> str:
    return f"S1A_IW_GRDH_1SDV_STUB{n:02d}_{region_id.upper().replace('-', '')}"


def discover(ctx: StageContext, request: DiscoverRequest) -> DiscoverResult:
    region = ctx.config.regions[request.region_id]
    ids = [stub_scene_id(region.region_id, n) for n in range(1, STUB_SCENES_PER_REGION + 1)]
    base = datetime(2026, 1, 1, 5, 30, tzinfo=UTC)
    new: list[str] = []
    with ctx.db.connection() as conn:
        for n, scene_id in enumerate(ids):
            if db.insert_scene(conn, scene_id, region.region_id, base + timedelta(days=n), region.polygon):
                new.append(scene_id)
    return DiscoverResult(new_scene_ids=new, known_scene_count=len(ids) - len(new))


def fetch(ctx: StageContext, request: SceneStageRequest) -> FetchResult:
    return FetchResult(raster_key=f"scenes/{request.scene_id}/vv.tif", bytes_downloaded=0)


def detect(ctx: StageContext, request: DetectRequest) -> DetectResult:
    region = region_for_scene(ctx, request.scene_id)
    name = request.detector.name if request.detector else region.detector.name
    version = (request.detector.version if request.detector else None) or region.detector.version
    lons = [p[0] for p in region.polygon]
    lats = [p[1] for p in region.polygon]
    mid_lon, mid_lat = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2
    candidates: list[dict[str, Any]] = [
        {"lon": mid_lon, "lat": mid_lat, "confidence": 0.9, "length_m": 180.0, "shore": "open_water"},
        {
            "lon": mid_lon + 0.05,
            "lat": mid_lat + 0.05,
            "confidence": 0.6,
            "length_m": 45.0,
            "shore": "near_shore",
        },
    ]
    with ctx.db.connection() as conn:
        db.replace_detections(conn, request.scene_id, name, version, candidates)
    return DetectResult(
        detection_count=len(candidates), dropped_on_land=0, detector_name=name, detector_version=version
    )


def ais_load(ctx: StageContext, request: AisLoadRequest) -> AisLoadResult:
    region = ctx.config.regions[request.region_id]
    noon = datetime(request.date.year, request.date.month, request.date.day, 12, tzinfo=UTC)
    lons = [p[0] for p in region.polygon]
    lats = [p[1] for p in region.polygon]
    positions = [
        {
            "mmsi": STUB_MMSI + i,
            "ts": noon + timedelta(minutes=i),
            "lon": min(lons) + 0.1,
            "lat": min(lats) + 0.1,
        }
        for i in range(3)
    ]
    with ctx.db.connection() as conn:
        db.insert_ais_positions(conn, positions)
    return AisLoadResult(positions_loaded=len(positions))


def validate(ctx: StageContext, request: SceneStageRequest) -> ValidateResult:
    region = region_for_scene(ctx, request.scene_id)
    gate = region.validation.gate_m
    matched = unmatched = 0
    with ctx.db.connection() as conn:
        for i, detection_id in enumerate(db.scene_detection_ids(conn, request.scene_id)):
            if i % 2 == 0:
                db.upsert_validation(conn, detection_id, "matched", STUB_MMSI, 120.5, gate)
                matched += 1
            else:
                db.upsert_validation(conn, detection_id, "unmatched", None, None, gate)
                unmatched += 1
    return ValidateResult(matched=matched, unmatched=unmatched, ambiguous=0)
