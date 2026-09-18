"""The tool gateway: a separate process because it is a security boundary.

It exists so the investigation worker never holds a telemetry credential, a
Kubernetes token or a deploy API key. The worker sends a capability token and a
tool name; the gateway decides whether that is allowed, calls the backend with
credentials the worker cannot see, redacts and compresses the result, and writes
an audit record.

Splitting it out also lets the eval harness point the same worker at a replayed
capsule by changing one URL.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from faultline import __version__
from faultline.config import Settings, get_settings
from faultline.gateway.backends.scenario import SCENARIOS
from faultline.gateway.policy import Capability, PolicyError, TokenSigner
from faultline.gateway.registry import ToolError, ToolRegistry
from faultline.logging import configure_logging, get_logger

log = get_logger(__name__)


class ToolCall(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def create_app(
    settings: Settings | None = None, scenario_name: str = "bad_deploy_checkout"
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.environment != "local")

    signer = TokenSigner(settings.gateway_signing_key)
    registry = ToolRegistry(SCENARIOS[scenario_name]())

    app = FastAPI(title="Faultline tool gateway", version=__version__)

    def capability(
        x_capability_token: Annotated[str | None, Header()] = None,
    ) -> Capability:
        if not x_capability_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="missing capability token"
            )
        try:
            return signer.read_capability(x_capability_token)
        except PolicyError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/tools/call")
    async def call_tool(
        body: ToolCall, cap: Annotated[Capability, Depends(capability)]
    ) -> dict[str, Any]:
        try:
            evidence = registry.call(body.tool, body.arguments, cap)
        except PolicyError as exc:
            # Policy denials are never retried; 403 tells the worker to stop asking.
            log.warning("tool_denied", tool=body.tool, incident=cap.incident_id, reason=str(exc))
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ToolError as exc:
            # A backend that cannot answer becomes an evidence gap, not a crash.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        log.info(
            "tool_called",
            tool=body.tool,
            incident=cap.incident_id,
            evidence_id=evidence.id,
            flagged=evidence.injection_flagged,
        )
        return evidence.model_dump(mode="json")

    return app


app = create_app()
