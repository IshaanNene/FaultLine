"""The API entrypoint.

Stateless by construction: every request's durable effect goes through the
repository, so pods scale on request rate and can be replaced mid-investigation
without anyone noticing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from faultline import __version__
from faultline.api.deps import build_container
from faultline.api.routes import health, incidents, webhook
from faultline.config import Settings, get_settings
from faultline.logging import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.environment != "local")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = build_container(settings)
        yield

    app = FastAPI(
        title="Faultline",
        version=__version__,
        summary="Agentic root-cause investigation for Kubernetes microservices.",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(incidents.router)
    return app


app = create_app()
