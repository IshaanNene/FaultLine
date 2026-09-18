"""Liveness and readiness.

Split deliberately: liveness must not depend on Postgres, or a database blip
restarts every pod at the worst possible moment.
"""

from __future__ import annotations

from fastapi import APIRouter

from faultline import __version__
from faultline.api.deps import ContainerDep

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
async def readyz(container: ContainerDep) -> dict[str, object]:
    checks: dict[str, object] = {"backend": container.settings.backend}
    try:
        checks["queue_pending"] = await container.bus.pending_count(
            container.settings.jobs_stream, container.settings.consumer_group
        )
        checks["ready"] = True
    except Exception as exc:
        checks["ready"] = False
        checks["error"] = str(exc)
    return checks
