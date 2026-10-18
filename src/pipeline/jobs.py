"""Async job framework: submit (idempotent), execute with registered handlers, recover after a crash.

The `jobs` table is the queue and the source of truth, so queued Jobs survive restarts and workers
need no extra broker. Workers claim Jobs with `FOR UPDATE SKIP LOCKED`.

Restart policy: Jobs left `running` by a crash are **failed as retryable** ("interrupted by service
restart"), never silently re-queued, so a poison job cannot crash-loop the service. The caller (Airflow)
retries with a bounded retry count, which creates a new Job. This assumes a single `pipeline-api`
instance: a second instance starting up would fail the first one's in-flight Jobs.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import psycopg
from pydantic import BaseModel

from contracts.protocols import PermanentError, TransientError
from pipeline import db, metrics
from pipeline.api.config import AppConfig
from pipeline.api.models import (
    AisLoadRequest,
    AisLoadResult,
    DetectorRef,
    DetectRequest,
    FetchResult,
    Job,
    Stage,
)
from pipeline.db import Database, Row
from pipeline.stages import (
    REQUEST_MODELS,
    RESULT_MODELS,
    SCENE_STEPS,
    Registry,
    StageContext,
)

log = logging.getLogger("pipeline.jobs")

MAX_ERROR_CHARS = 2000


class NotFoundError(Exception):
    """A Region or Scene referenced by a request does not exist (HTTP 404)."""


class WrongStatusError(Exception):
    """The Scene's status does not allow the stage (HTTP 409)."""


def job_from_row(row: Row) -> Job:
    return Job.model_validate(row)


def _scene_state(conn: db.Conn, scene_id: str) -> Row:
    row = conn.execute(
        "SELECT status::text AS status, failed_stage::text AS failed_stage, raster_key FROM scenes"
        " WHERE scene_id = %s",
        (scene_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"unknown scene {scene_id!r}")
    return row


def _check_scene_ready(conn: db.Conn, stage: Stage, scene_id: str) -> None:
    """Scene must be in the stage's required status, or have failed in this very stage (a retry)."""
    state = _scene_state(conn, scene_id)
    step = SCENE_STEPS[stage]
    is_retry = state["status"] == "failed" and state["failed_stage"] == stage.value
    # A processed Scene can be detected again while its raster is kept, and fetched again once it is gone.
    redo = stage in (Stage.DETECT, Stage.FETCH) and state["status"] in ("detected", "validated")
    if redo and (state["raster_key"] is not None) != (stage is Stage.DETECT):
        has = "no longer" if stage is Stage.DETECT else "still"
        raise WrongStatusError(f"scene {scene_id!r} is {state['status']} and {has} has a stored raster")
    if state["status"] != step.required.value and not is_retry and not redo:
        where = f" (failed in {state['failed_stage']})" if state["status"] == "failed" else ""
        raise WrongStatusError(
            f"scene {scene_id!r} is {state['status']}{where}; stage {stage.value} needs {step.required.value}"
        )


def submit_job(database: Database, config: AppConfig, stage: Stage, request: BaseModel) -> tuple[Job, bool]:
    """Idempotent submit. Returns (job, created).

    Order matters: 404 for unknown Region/Scene, then an existing queued/running/succeeded Job for the same
    stage and input wins (so a retried submit never gets a 409 from its own earlier success), then the 409
    status check, then a new Job.
    """
    input_ = request.model_dump(mode="json", exclude_none=True)
    if not all(db.storable(v) for v in _strings(input_)):
        # Valid per the schema (plain strings) but can never match anything we store, so: unknown.
        raise NotFoundError("request refers to an identifier containing characters that cannot exist")
    scene_id: str | None = getattr(request, "scene_id", None)
    with database.connection() as conn:
        if scene_id is not None:
            _scene_state(conn, scene_id)
        else:
            region_id: str = request.region_id  # type: ignore[attr-defined]
            if region_id not in config.regions:
                raise NotFoundError(f"unknown region {region_id!r}")
        if stage is Stage.DETECT and scene_id is not None and getattr(request, "detector", None) is None:
            # Pin the Region's detector version so a new version is a new Job instead of a dedupe hit.
            region = conn.execute("SELECT region_id FROM scenes WHERE scene_id = %s", (scene_id,)).fetchone()
            det = config.regions[region["region_id"]].detector if region else None
            if det is not None:
                input_ = {**input_, "detector": {"name": det.name, "version": det.version}}
                request = DetectRequest(
                    scene_id=scene_id, detector=DetectorRef(name=det.name, version=det.version)
                )
        existing = db.find_live_job(conn, stage.value, input_)
        if existing is not None:
            return job_from_row(existing), False
        if scene_id is not None and stage in SCENE_STEPS:
            _check_scene_ready(conn, stage, scene_id)
        row, created = db.insert_job(conn, stage.value, input_)
    return job_from_row(row), created


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)


def get_job(database: Database, job_id: str) -> Job | None:
    try:
        with database.connection() as conn:
            row = db.get_job(conn, job_id)
    except (psycopg.errors.InvalidTextRepresentation, psycopg.errors.DataError):
        return None  # not a uuid, so certainly not a Job
    return None if row is None else job_from_row(row)


def _error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:MAX_ERROR_CHARS]


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, PermanentError | WrongStatusError):
        return False
    # Network and database hiccups are transient; any other exception is a bug and retrying won't fix it.
    return isinstance(exc, TransientError | psycopg.OperationalError | OSError | TimeoutError)


class JobRunner:
    """Runs queued Jobs through the registered stage handlers on background threads."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        registry: Registry,
        *,
        workers: int = 2,
        poll_interval_s: float = 0.5,
    ) -> None:
        self._db = database
        self._ctx = StageContext(db=database, config=config)
        self._registry = registry
        self._workers = workers
        self._poll = poll_interval_s
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        self.recover_interrupted()
        for i in range(self._workers):
            t = threading.Thread(target=self._loop, name=f"job-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=10)
        self._threads.clear()

    def notify(self) -> None:
        """Wake an idle worker; called after a Job is created."""
        self._wake.set()

    def recover_interrupted(self) -> int:
        """Fail Jobs left `running` by a crash (retryable). Returns how many."""
        with self._db.connection() as conn:
            jobs = db.running_jobs(conn)
        for job in jobs:
            self._fail(job, "interrupted by service restart", retryable=True)
        return len(jobs)

    # --- execution -------------------------------------------------------------------------------

    def run_pending(self) -> int:
        """Run queued Jobs on the calling thread until none are left (used by tests and one-off runs)."""
        n = 0
        while self.run_next():
            n += 1
        return n

    def run_next(self) -> bool:
        with self._db.connection() as conn:
            job = db.claim_next_job(conn)
        if job is None:
            return False
        self._execute(job)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_next()
            except Exception:
                log.exception("job worker error")
                worked = False
            if not worked:
                self._wake.wait(self._poll)
                self._wake.clear()

    def _execute(self, job: Row) -> None:
        stage = Stage(job["stage"])
        started = time.monotonic()
        try:
            request = REQUEST_MODELS[stage].model_validate(job["input"])
            scene_id: str | None = getattr(request, "scene_id", None)
            if scene_id is not None and stage in SCENE_STEPS:
                with self._db.connection() as conn:
                    _check_scene_ready(conn, stage, scene_id)  # may have changed since submit
            result = self._registry[stage](self._ctx, request)
            if not isinstance(result, RESULT_MODELS[stage]):
                raise PermanentError(
                    f"handler for {stage.value} returned {type(result).__name__}, "
                    f"expected {RESULT_MODELS[stage].__name__}"
                )
            self._succeed(job, stage, request, result)
            metrics.record_job_finished(stage.value, "succeeded", time.monotonic() - started)
        except Exception as exc:
            metrics.record_job_finished(stage.value, "failed", time.monotonic() - started)
            log.warning("job %s (%s) failed: %s", job["id"], stage.value, _error_text(exc))
            # A stale Job (Scene moved on since submit) must not clobber the Scene's current status.
            self._fail(
                job,
                _error_text(exc),
                retryable=_is_retryable(exc),
                mark_scene=not isinstance(exc, WrongStatusError),
            )

    def _succeed(self, job: Row, stage: Stage, request: BaseModel, result: BaseModel) -> None:
        with self._db.connection() as conn:
            scene_id: str | None = getattr(request, "scene_id", None)
            if stage in SCENE_STEPS and scene_id is not None:
                raster_key = result.raster_key if isinstance(result, FetchResult) else None
                db.mark_scene_done(conn, scene_id, SCENE_STEPS[stage].done.value, raster_key)
            elif isinstance(request, AisLoadRequest) and isinstance(result, AisLoadResult):
                db.mark_ais_day_loaded(conn, request.region_id, request.date, result.positions_loaded)
            db.finish_job_succeeded(conn, job["id"], result.model_dump(mode="json", exclude_none=True))

    def _fail(self, job: Row, error: str, *, retryable: bool, mark_scene: bool = True) -> None:
        stage = Stage(job["stage"])
        scene_id: Any = job["input"].get("scene_id")
        with self._db.connection() as conn:
            db.finish_job_failed(conn, job["id"], error, retryable)
            if mark_scene and stage in SCENE_STEPS and isinstance(scene_id, str):
                db.mark_scene_failed(conn, scene_id, stage.value, error)
