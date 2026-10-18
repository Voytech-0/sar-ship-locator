"""Region configuration (mirrors `contracts/config.schema.json`) and service settings from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

import contracts

DEFAULT_REGION_CONFIG = Path(contracts.__file__).resolve().parent / "region.greatbelt.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoveryConfig(_Strict):
    lookback_days: int = Field(ge=1)


class DetectorConfig(_Strict):
    name: str
    version: str
    params: dict[str, Any]


class LandMaskConfig(_Strict):
    near_shore_m: float = Field(default=300, ge=0)  # kept but flagged `near_shore`
    exclusion_m: float = Field(default=0, ge=0)  # ignored like land


class ValidationConfig(_Strict):
    gate_m: float = Field(gt=0)
    ais_margin_minutes: int = Field(ge=0)
    # Height of the sea surface above the ellipsoid the product is geolocated on (the geoid undulation).
    sea_level_height_m: float = 0.0


class RetentionConfig(_Strict):
    raster_days: int = Field(
        ge=0
    )  # days to keep the cropped raster after the Scene's last stage; 0 = delete at once


class RegionConfig(_Strict):
    region_id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    polygon: list[tuple[float, float]] = Field(min_length=4)  # exterior ring, [lon, lat], closed
    discovery: DiscoveryConfig
    detector: DetectorConfig
    land_mask: LandMaskConfig = LandMaskConfig()
    validation: ValidationConfig
    retention: RetentionConfig

    @field_validator("polygon")
    @classmethod
    def _closed(cls, ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if ring[0] != ring[-1]:
            raise ValueError("polygon ring must be closed (first point repeated at the end)")
        return ring


@dataclass(frozen=True)
class AppConfig:
    regions: dict[str, RegionConfig]


def load_region_config(path: Path | str) -> RegionConfig:
    return RegionConfig.model_validate(yaml.safe_load(Path(path).read_text()))


def load_app_config(paths: list[Path]) -> AppConfig:
    regions = [load_region_config(p) for p in paths]
    ids = [r.region_id for r in regions]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate region_id in region configs: {ids}")
    return AppConfig(regions={r.region_id: r for r in regions})


@dataclass(frozen=True)
class Settings:
    """Service settings. `from_env` reads `PIPELINE_*` variables."""

    database_url: str
    region_config_paths: list[Path] = field(default_factory=lambda: [DEFAULT_REGION_CONFIG])
    workers: int = 2  # background job threads; the S3 link is bandwidth-bound, so keep this low
    poll_interval_s: float = 0.5

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = dict(os.environ) if env is None else env
        if "PIPELINE_DATABASE_URL" not in e:
            raise RuntimeError("PIPELINE_DATABASE_URL is not set")
        paths = [Path(p) for p in e.get("PIPELINE_REGION_CONFIGS", "").split(",") if p.strip()]
        return cls(
            database_url=e["PIPELINE_DATABASE_URL"],
            region_config_paths=paths or [DEFAULT_REGION_CONFIG],
            workers=int(e.get("PIPELINE_WORKERS", "2")),
            poll_interval_s=float(e.get("PIPELINE_POLL_INTERVAL", "0.5")),
        )
