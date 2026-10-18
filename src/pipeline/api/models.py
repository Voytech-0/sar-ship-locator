"""Pydantic models mirroring `contracts/openapi.yaml`. The YAML is the source of truth; keep these in step."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RFC3339_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Stage(StrEnum):
    DISCOVER = "discover"
    FETCH = "fetch"
    DETECT = "detect"
    AIS_LOAD = "ais-load"
    VALIDATE = "validate"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SceneStatus(StrEnum):
    DISCOVERED = "discovered"
    FETCHED = "fetched"
    DETECTED = "detected"
    VALIDATED = "validated"
    FAILED = "failed"


class MatchLabel(StrEnum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"


class ShoreClass(StrEnum):
    OPEN_WATER = "open_water"
    NEAR_SHORE = "near_shore"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- requests -------------------------------------------------------------------------------------


class DiscoverRequest(Strict):
    region_id: str
    since: datetime | None = None

    @field_validator("since", mode="before")
    @classmethod
    def _rfc3339(cls, v: Any) -> Any:
        # The schema says `format: date-time` (a string with a UTC offset); pydantic would also accept
        # numbers (epoch seconds) and naive timestamps, which the contract does not allow.
        if v is not None and not (isinstance(v, str) and RFC3339_DATE_TIME.match(v)):
            raise ValueError("must be an RFC 3339 date-time string with a UTC offset")
        return v

    @field_validator("since")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        # Normalise so equal instants give equal Job input (idempotency key).
        return None if v is None else v.astimezone(UTC)


class SceneStageRequest(Strict):
    scene_id: str
    force: bool = False  # run again although an identical Job succeeded; not part of the Job input


class DetectorRef(Strict):
    name: str
    version: str | None = None


class DetectRequest(Strict):
    scene_id: str
    detector: DetectorRef | None = None
    force: bool = False


class AisLoadRequest(Strict):
    region_id: str
    date: date

    @field_validator("date", mode="before")
    @classmethod
    def _iso_date(cls, v: Any) -> Any:
        if not (isinstance(v, str) and ISO_DATE.match(v)):
            raise ValueError("must be a YYYY-MM-DD date string")
        return v


# --- stage results --------------------------------------------------------------------------------


class DiscoverResult(BaseModel):
    new_scene_ids: list[str]
    known_scene_count: int = Field(ge=0)


class FetchResult(BaseModel):
    raster_key: str
    bytes_downloaded: int = Field(ge=0)


class DetectResult(BaseModel):
    detection_count: int = Field(ge=0)
    dropped_on_land: int | None = Field(default=None, ge=0)
    detector_name: str
    detector_version: str


class AisLoadResult(BaseModel):
    positions_loaded: int = Field(ge=0)


class ValidateResult(BaseModel):
    matched: int = Field(ge=0)
    unmatched: int = Field(ge=0)
    ambiguous: int = Field(ge=0)


# --- responses ------------------------------------------------------------------------------------


class Polygon(BaseModel):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[list[float]]]


class Point(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: list[float]


class Job(BaseModel):
    id: str
    stage: Stage
    input: dict[str, Any]
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    error_retryable: bool | None = None
    result: dict[str, Any] | None = None


class Region(BaseModel):
    region_id: str
    name: str
    polygon: Polygon


class Scene(BaseModel):
    scene_id: str
    region_id: str
    acquired_at: datetime
    status: SceneStatus
    failed_stage: Stage | None = None
    error: str | None = None
    footprint: Polygon
    raster_key: str | None = None
    detection_count: int | None = Field(default=None, ge=0)
    discovered_at: datetime
    updated_at: datetime


class ScenePage(BaseModel):
    items: list[Scene]
    total: int = Field(ge=0)


class DetectionProperties(BaseModel):
    id: str
    scene_id: str
    detector_name: str
    detector_version: str
    confidence: float = Field(ge=0, le=1)
    length_m: float | None = Field(default=None, ge=0)
    shore_class: ShoreClass
    chip_key: str | None = None
    label: MatchLabel | None = None
    matched_mmsi: int | None = None
    match_distance_m: float | None = Field(default=None, ge=0)


class DetectionFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: Point
    properties: DetectionProperties


class DetectionCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[DetectionFeature]


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
