"""Stage handler interface, registry and the Scene status machine.

A **handler** does the work of one stage. It receives a `StageContext` and the validated request model,
and returns the stage's typed result (the result models in `pipeline.api.models`). Handlers write their own
domain rows (Scenes for `discover`, Detections, Validations, AIS Positions) using `pipeline.db`.

The framework (`pipeline.jobs`) owns everything around a handler:
  * Job state (queued/running/succeeded/failed) and idempotency,
  * the Scene status transition on success (`SCENE_STEPS`), clearing any earlier failure,
  * marking the Scene `failed` when a handler raises,
  * `ais_loaded_days` bookkeeping after `ais-load` succeeds,
  * mapping exceptions to `error_retryable` (`TransientError` retryable, `PermanentError` not).

Handlers should be idempotent: re-running the same stage on the same input must leave the same data.
Raise `contracts.protocols.TransientError` / `PermanentError` to control retries.

To plug in real implementations, build a registry with `default_registry()` and override entries:

    registry = {**default_registry(), Stage.DETECT: my_detect_handler}
    create_app(settings, registry=registry)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from pipeline.api.config import AppConfig, RegionConfig
from pipeline.api.models import (
    AisLoadRequest,
    AisLoadResult,
    DetectRequest,
    DetectResult,
    DiscoverRequest,
    DiscoverResult,
    FetchResult,
    SceneStageRequest,
    SceneStatus,
    Stage,
    ValidateResult,
)
from pipeline.db import Database

__all__ = [
    "REQUEST_MODELS",
    "RESULT_MODELS",
    "SCENE_STEPS",
    "SceneStep",
    "StageContext",
    "StageHandler",
    "default_registry",
    "region_for_scene",
]


@dataclass(frozen=True)
class StageContext:
    """What a handler gets besides its request: database access and the loaded configuration."""

    db: Database
    config: AppConfig


# (context, validated request model) -> result model of the stage
StageHandler = Callable[[StageContext, Any], BaseModel]
Registry = Mapping[Stage, StageHandler]

REQUEST_MODELS: dict[Stage, type[BaseModel]] = {
    Stage.DISCOVER: DiscoverRequest,
    Stage.FETCH: SceneStageRequest,
    Stage.DETECT: DetectRequest,
    Stage.AIS_LOAD: AisLoadRequest,
    Stage.VALIDATE: SceneStageRequest,
}

RESULT_MODELS: dict[Stage, type[BaseModel]] = {
    Stage.DISCOVER: DiscoverResult,
    Stage.FETCH: FetchResult,
    Stage.DETECT: DetectResult,
    Stage.AIS_LOAD: AisLoadResult,
    Stage.VALIDATE: ValidateResult,
}


@dataclass(frozen=True)
class SceneStep:
    """Scene status machine edge: a stage needs `required` and, on success, moves the Scene to `done`.

    A Scene that `failed` in this same stage may also be re-run (success then clears the failure).
    """

    required: SceneStatus
    done: SceneStatus


SCENE_STEPS: dict[Stage, SceneStep] = {
    Stage.FETCH: SceneStep(SceneStatus.DISCOVERED, SceneStatus.FETCHED),
    Stage.DETECT: SceneStep(SceneStatus.FETCHED, SceneStatus.DETECTED),
    Stage.VALIDATE: SceneStep(SceneStatus.DETECTED, SceneStatus.VALIDATED),
}


def region_for_scene(ctx: StageContext, scene_id: str) -> RegionConfig:
    """The configuration of the Region a Scene belongs to."""
    with ctx.db.connection() as conn:
        row = conn.execute("SELECT region_id FROM scenes WHERE scene_id = %s", (scene_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown scene {scene_id}")
    return ctx.config.regions[row["region_id"]]


def default_registry() -> dict[Stage, StageHandler]:
    """Stub handlers returning canned, schema-valid results. Replace entries as real stages land."""
    from pipeline.stages import stubs

    return {
        Stage.DISCOVER: stubs.discover,
        Stage.FETCH: stubs.fetch,
        Stage.DETECT: stubs.detect,
        Stage.AIS_LOAD: stubs.ais_load,
        Stage.VALIDATE: stubs.validate,
    }
