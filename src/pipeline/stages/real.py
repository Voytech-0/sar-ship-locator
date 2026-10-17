"""Real stage handlers, built over the adapters and algorithms (`Services`).

`make_registry(services)` returns a registry for `create_app`. Every handler is idempotent: re-running a
stage on the same input leaves the same data. Failures are raised as the contract's Transient/Permanent
errors by the adapters and mapped to Job state by `pipeline.jobs`.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from contracts.protocols import (
    AisPosition,
    AisSource,
    CatalogueClient,
    DetectionRef,
    Detector,
    LandMask,
    ObjectStore,
    PermanentError,
    Region,
    SceneFetcher,
    SceneRef,
    ShoreClass,
    Validator,
)
from pipeline import db, metrics
from pipeline.api.config import RegionConfig
from pipeline.api.models import (
    AisLoadRequest,
    AisLoadResult,
    DetectRequest,
    DetectResult,
    DiscoverRequest,
    DiscoverResult,
    FetchResult,
    SceneStageRequest,
    Stage,
    ValidateResult,
)
from pipeline.stages import StageContext, StageHandler, region_for_scene
from pipeline.stages.chips import chip_npy, chip_png, cut_chip
from pipeline.stages.preview import make_preview, save_preview
from pipeline.stages.raster_store import delete_raster, load_raster, save_raster

log = logging.getLogger(__name__)

AIS_BATCH = 20_000


@contextmanager
def _source(name: str) -> Iterator[None]:
    """Count a call to an external data source as ok or error."""
    try:
        yield
    except Exception:
        metrics.observe_source_request(name, "error")
        raise
    metrics.observe_source_request(name, "ok")


@dataclass(frozen=True)
class Services:
    catalogue: CatalogueClient
    fetcher: SceneFetcher
    ais_source: AisSource
    store: ObjectStore
    land_mask: Callable[[RegionConfig], LandMask]
    detectors: Mapping[str, Callable[[Mapping[str, Any]], Detector]]  # name -> factory(params)
    validator: Validator


def to_region(cfg: RegionConfig) -> Region:
    return Region(region_id=cfg.region_id, name=cfg.name, polygon=list(cfg.polygon))


def make_registry(services: Services) -> dict[Stage, StageHandler]:
    def discover(ctx: StageContext, request: DiscoverRequest) -> DiscoverResult:
        cfg = ctx.config.regions[request.region_id]
        since = request.since or datetime.now(UTC) - timedelta(days=cfg.discovery.lookback_days)
        with _source("cdse"):
            refs = services.catalogue.search(to_region(cfg), since)
        new: list[str] = []
        with ctx.db.connection() as conn:
            for ref in refs:
                if db.insert_scene(conn, ref.scene_id, cfg.region_id, ref.acquired_at, ref.footprint):
                    new.append(ref.scene_id)
        metrics.scenes_discovered(cfg.region_id, len(new))
        return DiscoverResult(new_scene_ids=new, known_scene_count=len(refs) - len(new))

    def fetch(ctx: StageContext, request: SceneStageRequest) -> FetchResult:
        cfg = region_for_scene(ctx, request.scene_id)
        with ctx.db.connection() as conn:
            row = db.get_scene(conn, request.scene_id)
            start = db.get_scene_start(conn, request.scene_id)  # not row["acquired_at"]: fetch rewrites that
        assert row is not None
        ring = [(float(x), float(y)) for x, y in row["footprint"]["coordinates"][0]]
        with _source("cdse"):
            raster = services.fetcher.fetch(SceneRef(request.scene_id, start, ring), to_region(cfg))
        metrics.bytes_downloaded("cdse", raster.bytes_downloaded)
        key = save_raster(services.store, raster)
        with ctx.db.connection() as conn:  # the Region data's own time, used for AIS matching
            db.set_scene_acquired_at(conn, request.scene_id, raster.acquired_at)
        return FetchResult(raster_key=key, bytes_downloaded=raster.bytes_downloaded)

    def detect(ctx: StageContext, request: DetectRequest) -> DetectResult:
        cfg = region_for_scene(ctx, request.scene_id)
        pinned = request.detector
        name = pinned.name if pinned else cfg.detector.name
        other = pinned is not None and pinned.name != cfg.detector.name
        # the Region config's version labels its parameter set; it is stored with every Detection
        params = {} if other else {**cfg.detector.params, "version": cfg.detector.version}
        if name not in services.detectors:
            raise PermanentError(f"unknown detector {name!r}")
        detector = services.detectors[name](params)
        if pinned and pinned.version and pinned.version != detector.version:
            raise PermanentError(f"detector {name} is version {detector.version}, not {pinned.version}")

        raster = load_raster(services.store, request.scene_id)
        land = services.land_mask(cfg)
        # Detect everywhere except land, so near-shore ships are flagged rather than lost.
        raster = dataclasses.replace(raster, water_mask=land.not_land_mask(raster))
        candidates = detector.detect(raster)

        kept: list[tuple[Any, ShoreClass]] = []
        dropped = 0
        for cand in candidates:
            shore = land.classify(cand.position)
            if shore is ShoreClass.ON_LAND:
                dropped += 1
            else:
                kept.append((cand, shore))

        rows = [
            {
                "lon": c.position[0],
                "lat": c.position[1],
                "confidence": c.confidence,
                "length_m": c.length_m,
                "shore": shore.value,
            }
            for c, shore in kept
        ]
        with ctx.db.connection() as conn:
            ids = db.replace_detections(conn, request.scene_id, detector.name, detector.version, rows)
        chip_keys: dict[str, str] = {}
        for det_id, (cand, _) in zip(ids, kept, strict=True):
            chip = cut_chip(raster.sigma0_vv, cand.pixel_box)
            base = f"chips/{request.scene_id}/{det_id}"
            services.store.put(f"{base}.png", chip_png(chip), "image/png")
            services.store.put(f"{base}.npy", chip_npy(chip), "application/octet-stream")
            chip_keys[det_id] = f"{base}.png"
        with ctx.db.connection() as conn:
            db.set_chip_keys(conn, chip_keys)
        preview = make_preview(raster)  # the raster is deleted below, so the preview is made now
        if preview:
            save_preview(services.store, request.scene_id, preview)
        if cfg.retention.delete_raster_after_detect:
            delete_raster(services.store, request.scene_id)
        for shore_name in ("open_water", "near_shore"):
            n = sum(1 for _, s in kept if s.value == shore_name)
            if n:
                metrics.detections_written(cfg.region_id, detector.name, shore_name, n)
        metrics.detections_dropped_on_land(cfg.region_id, detector.name, dropped)
        return DetectResult(
            detection_count=len(ids),
            dropped_on_land=dropped,
            detector_name=detector.name,
            detector_version=detector.version,
        )

    def ais_load(ctx: StageContext, request: AisLoadRequest) -> AisLoadResult:
        cfg = ctx.config.regions[request.region_id]
        loaded = 0
        batch: list[dict[str, Any]] = []
        with _source("dma"), ctx.db.connection() as conn:
            for p in services.ais_source.positions(request.date, to_region(cfg)):  # errors counted below
                batch.append(
                    {
                        "mmsi": p.mmsi,
                        "ts": p.ts,
                        "lon": p.position[0],
                        "lat": p.position[1],
                        "sog_knots": p.sog_knots,
                        "cog_deg": p.cog_deg,
                        "heading": p.heading,
                    }
                )
                if len(batch) >= AIS_BATCH:
                    loaded += db.insert_ais_positions(conn, batch)
                    batch = []
            loaded += db.insert_ais_positions(conn, batch)
        metrics.ais_positions_loaded(cfg.region_id, loaded)
        return AisLoadResult(positions_loaded=loaded)

    def validate(ctx: StageContext, request: SceneStageRequest) -> ValidateResult:
        cfg = region_for_scene(ctx, request.scene_id)
        with ctx.db.connection() as conn:
            scene = db.get_scene(conn, request.scene_id)
            assert scene is not None
            when = scene["acquired_at"]
            margin = timedelta(minutes=cfg.validation.ais_margin_minutes)
            dets = db.list_detection_points(conn, request.scene_id)
            positions = db.list_ais_positions(conn, when - margin, when + margin)
        results = services.validator.validate(
            [DetectionRef(d["id"], (d["lon"], d["lat"]), d["length_m"]) for d in dets],
            [
                AisPosition(
                    p["mmsi"], p["ts"], (p["lon"], p["lat"]), p["sog_knots"], p["cog_deg"], p["heading"]
                )
                for p in positions
            ],
            when,
            cfg.validation.gate_m,
        )
        counts = {"matched": 0, "unmatched": 0, "ambiguous": 0}
        with ctx.db.connection() as conn:
            for r in results:
                db.upsert_validation(
                    conn,
                    r.detection_id,
                    r.label.value,
                    r.matched_mmsi,
                    r.match_distance_m,
                    cfg.validation.gate_m,
                )
                counts[r.label.value] += 1
        for label, n in counts.items():
            if n:
                metrics.validations_recorded(cfg.region_id, label, n)
        return ValidateResult(**counts)

    return {
        Stage.DISCOVER: discover,
        Stage.FETCH: fetch,
        Stage.DETECT: detect,
        Stage.AIS_LOAD: ais_load,
        Stage.VALIDATE: validate,
    }
