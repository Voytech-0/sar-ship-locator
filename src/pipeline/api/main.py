"""Production entry point: the API with real stage handlers (`uvicorn --factory pipeline.api.main:app`)."""

from __future__ import annotations

from fastapi import FastAPI

from pipeline.api.app import create_app
from pipeline.stages.build import build_services
from pipeline.stages.real import make_registry


def app() -> FastAPI:
    services = build_services()

    def object_store_ping() -> None:
        services.store.exists("healthcheck")  # raises if the store is unreachable

    return create_app(registry=make_registry(services), health_checks={"object_store": object_store_ping})
