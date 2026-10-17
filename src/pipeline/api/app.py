"""The pipeline FastAPI service, implementing `contracts/openapi.yaml`.

Run it with `uvicorn --factory pipeline.api.app:create_app` (settings come from `PIPELINE_*` variables,
see `pipeline.api.config.Settings`). The schema is applied to the database by the migrations, not here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from pipeline import db, jobs, metrics
from pipeline.api.config import AppConfig, Settings, load_app_config
from pipeline.api.models import (
    AisLoadRequest,
    DetectionCollection,
    DetectionFeature,
    DetectionProperties,
    DetectRequest,
    DiscoverRequest,
    Job,
    Point,
    Polygon,
    Problem,
    Region,
    Scene,
    ScenePage,
    SceneStageRequest,
    SceneStatus,
    Stage,
)
from pipeline.db import Database
from pipeline.jobs import JobRunner, NotFoundError, WrongStatusError
from pipeline.stages import Registry, default_registry

log = logging.getLogger("pipeline.api")

PROBLEM_JSON = "application/problem+json"


def problem(
    status: int, title: str, detail: str | None = None, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    body = Problem(title=title, status=status, detail=detail)
    return JSONResponse(
        body.model_dump(exclude_none=True), status_code=status, media_type=PROBLEM_JSON, headers=headers
    )


def _unstorable(*values: str | None) -> bool:
    return any(v is not None and not db.storable(v) for v in values)


def _scene(row: db.Row) -> Scene:
    return Scene.model_validate(row)


def _feature(row: db.Row) -> DetectionFeature:
    props = {k: row[k] for k in DetectionProperties.model_fields}
    return DetectionFeature(
        geometry=Point.model_validate(row["geometry"]), properties=DetectionProperties(**props)
    )


def _region(config: AppConfig, region_id: str) -> Region:
    r = config.regions[region_id]
    return Region(
        region_id=r.region_id,
        name=r.name,
        polygon=Polygon(coordinates=[[[lon, lat] for lon, lat in r.polygon]]),
    )


def build_router() -> APIRouter:
    router = APIRouter(prefix="/v1")

    def submit(request: Request, stage: Stage, body: Any) -> JSONResponse:
        st = request.app.state
        job, created = jobs.submit_job(st.db, st.config, stage, body)
        if created:
            st.runner.notify()
        return JSONResponse(job.model_dump(mode="json"), status_code=202 if created else 200)

    @router.post("/stages/discover", status_code=202, response_model=Job)
    def submit_discover(request: Request, body: DiscoverRequest) -> JSONResponse:
        return submit(request, Stage.DISCOVER, body)

    @router.post("/stages/fetch", status_code=202, response_model=Job)
    def submit_fetch(request: Request, body: SceneStageRequest) -> JSONResponse:
        return submit(request, Stage.FETCH, body)

    @router.post("/stages/detect", status_code=202, response_model=Job)
    def submit_detect(request: Request, body: DetectRequest) -> JSONResponse:
        return submit(request, Stage.DETECT, body)

    @router.post("/stages/ais-load", status_code=202, response_model=Job)
    def submit_ais_load(request: Request, body: AisLoadRequest) -> JSONResponse:
        return submit(request, Stage.AIS_LOAD, body)

    @router.post("/stages/validate", status_code=202, response_model=Job)
    def submit_validate(request: Request, body: SceneStageRequest) -> JSONResponse:
        return submit(request, Stage.VALIDATE, body)

    @router.get("/jobs/{job_id}", response_model=Job)
    def get_job(request: Request, job_id: str) -> Job:
        job = None if _unstorable(job_id) else jobs.get_job(request.app.state.db, job_id)
        if job is None:
            raise NotFoundError(f"unknown job {job_id!r}")
        return job

    @router.get("/regions", response_model=list[Region])
    def list_regions(request: Request) -> list[Region]:
        config: AppConfig = request.app.state.config
        return [_region(config, rid) for rid in sorted(config.regions)]

    @router.get("/scenes", response_model=ScenePage)
    def list_scenes(
        request: Request,
        region_id: str | None = None,
        status: SceneStatus | None = None,
        ais_ready: bool | None = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> ScenePage:
        if _unstorable(region_id):
            return ScenePage(items=[], total=0)
        config: AppConfig = request.app.state.config
        margins = {rid: r.validation.ais_margin_minutes for rid, r in config.regions.items()}
        with request.app.state.db.connection() as conn:
            rows, total = db.list_scenes(
                conn,
                region_id=region_id,
                status=status.value if status else None,
                ais_ready=ais_ready,
                ais_margins=margins,
                limit=limit,
                offset=offset,
            )
        return ScenePage(items=[_scene(r) for r in rows], total=total)

    @router.get("/scenes/{scene_id}", response_model=Scene)
    def get_scene(request: Request, scene_id: str) -> Scene:
        row = None
        if not _unstorable(scene_id):
            with request.app.state.db.connection() as conn:
                row = db.get_scene(conn, scene_id)
        if row is None:
            raise NotFoundError(f"unknown scene {scene_id!r}")
        return _scene(row)

    @router.get("/scenes/{scene_id}/detections", response_model=DetectionCollection)
    def get_scene_detections(
        request: Request,
        scene_id: str,
        detector_name: str | None = None,
        detector_version: str | None = None,
    ) -> JSONResponse:
        if _unstorable(scene_id):
            raise NotFoundError(f"unknown scene {scene_id!r}")
        with request.app.state.db.connection() as conn:
            if db.get_scene(conn, scene_id) is None:
                raise NotFoundError(f"unknown scene {scene_id!r}")
            rows = (
                []
                if _unstorable(detector_name, detector_version)
                else db.list_detections(conn, scene_id, detector_name, detector_version)
            )
        body = DetectionCollection(features=[_feature(r) for r in rows])
        return JSONResponse(body.model_dump(mode="json"), media_type="application/geo+json")

    @router.get("/healthz")
    def healthz(request: Request) -> Response:
        checks: dict[str, Callable[[], None]] = request.app.state.health_checks
        failed: list[str] = []
        for name, check in checks.items():
            try:
                check()
            except Exception as exc:
                log.warning("health check %s failed: %s", name, type(exc).__name__)
                failed.append(f"{name} ({type(exc).__name__})")
        if failed:
            return problem(503, "Dependency unavailable", ", ".join(failed))
        return JSONResponse({"status": "ok"})

    @router.get("/metrics")
    def metrics_endpoint(request: Request) -> Response:
        try:
            _refresh_gauges(request.app.state.db, request.app.state.config)
        except psycopg.Error:
            log.warning("could not refresh gauges from the database", exc_info=True)
        return Response(metrics.render(), media_type=metrics.CONTENT_TYPE)

    return router


def _refresh_gauges(database: Database, config: AppConfig) -> None:
    """Gauges that mirror database state are set at scrape time, so they are always current."""
    now = datetime.now(UTC)
    with database.connection() as conn:
        counts: dict[str, dict[str, int]] = {rid: {} for rid in config.regions}
        for row in db.scene_status_counts(conn):
            counts.setdefault(row["region_id"], {})[row["status"]] = row["n"]
        in_flight = {row["stage"]: row["n"] for row in db.jobs_in_flight_counts(conn)}
        newest = {row["region_id"]: row["t"] for row in db.last_scene_acquired(conn)}
        ais_day = {row["region_id"]: row["d"] for row in db.latest_ais_day(conn)}
    for region_id, by_status in counts.items():
        metrics.set_scene_counts(region_id, by_status)
        if region_id in newest:
            metrics.set_last_scene_acquired(region_id, newest[region_id].timestamp())
        if region_id in ais_day:
            metrics.set_ais_lag(region_id, float((now.date() - ais_day[region_id]).days))
    for stage in metrics.STAGES:
        metrics.set_jobs_in_flight(stage, in_flight.get(stage, 0))


def create_app(
    settings: Settings | None = None,
    *,
    registry: Registry | None = None,
    start_workers: bool = True,
    health_checks: Mapping[str, Callable[[], None]] | None = None,
) -> FastAPI:
    """Build the service. `registry` overrides stage handlers (default: stubs); `start_workers=False`
    leaves Jobs queued so tests can drive them with `app.state.runner.run_pending()`."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or Settings.from_env()
        config = load_app_config(cfg.region_config_paths)
        database = Database(cfg.database_url)
        with database.connection() as conn:
            for r in config.regions.values():
                db.upsert_region(conn, r.region_id, r.name, r.polygon)
        runner = JobRunner(
            database,
            config,
            registry if registry is not None else default_registry(),
            workers=cfg.workers,
            poll_interval_s=cfg.poll_interval_s,
        )
        app.state.db = database
        app.state.config = config
        app.state.runner = runner
        app.state.health_checks = {"database": database.ping, **(health_checks or {})}
        if start_workers:
            runner.start()
        else:
            runner.recover_interrupted()
        try:
            yield
        finally:
            runner.stop()

    app = FastAPI(title="SAR Ship Locator pipeline API", version="1.0.0", lifespan=lifespan)
    app.include_router(build_router())

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return problem(422, "Invalid request", detail)

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return problem(404, "Not found", str(exc))

    @app.exception_handler(WrongStatusError)
    async def _wrong_status(_: Request, exc: WrongStatusError) -> JSONResponse:
        return problem(409, "Scene status does not allow this stage", str(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # An unparseable body (e.g. invalid UTF-8) arrives as 400; the contract's answer is 422.
        if exc.status_code == 400:
            return problem(422, "Invalid request", str(exc.detail))
        title = "Not found" if exc.status_code == 404 else "HTTP error"
        return problem(exc.status_code, title, str(exc.detail), headers=exc.headers)  # keeps `Allow` on 405

    @app.exception_handler(UnicodeError)
    @app.exception_handler(psycopg.errors.DataError)
    async def _bad_text(_: Request, exc: Exception) -> JSONResponse:
        return problem(422, "Invalid request", "request contains characters the database cannot store")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return problem(500, "Internal error")

    return app
