"""Production entry point: the API with real stage handlers (`uvicorn --factory pipeline.api.main:app`)."""

from __future__ import annotations

from fastapi import FastAPI

from pipeline.api.app import create_app
from pipeline.stages.build import build_services
from pipeline.stages.real import make_registry


def app() -> FastAPI:
    return create_app(registry=make_registry(build_services()))
