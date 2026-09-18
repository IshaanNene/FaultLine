"""Dependency wiring and the authentication shim.

The container is built once per process and holds the adapters chosen by
`Settings.backend`. Swapping `memory` for `live` is the only difference between
a test run and a deployed one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from faultline.adapters.memory import InMemoryBus, InMemoryEventPublisher, InMemoryRepository
from faultline.config import Settings, get_settings
from faultline.gateway.policy import TokenSigner
from faultline.ports import Bus, EventPublisher, Repository


class Role(StrEnum):
    VIEWER = "viewer"
    RESPONDER = "responder"
    APPROVER = "approver"
    ADMIN = "admin"


# Roles are cumulative: an approver can obviously read the incident they are
# being asked to approve. Written as an explicit table rather than an ordering,
# because the next role added may not be a superset of the last.
ROLE_IMPLIES: dict[Role, frozenset[Role]] = {
    Role.VIEWER: frozenset({Role.VIEWER}),
    Role.RESPONDER: frozenset({Role.RESPONDER, Role.VIEWER}),
    Role.APPROVER: frozenset({Role.APPROVER, Role.RESPONDER, Role.VIEWER}),
    Role.ADMIN: frozenset(Role),
}


@dataclass(slots=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[Role]

    @property
    def effective_roles(self) -> frozenset[Role]:
        return (
            frozenset().union(*(ROLE_IMPLIES[r] for r in self.roles)) if self.roles else frozenset()
        )

    def require(self, role: Role) -> None:
        if role not in self.effective_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"requires role {role}"
            )


@dataclass(slots=True)
class Container:
    settings: Settings
    bus: Bus
    repository: Repository
    publisher: EventPublisher
    signer: TokenSigner


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or get_settings()
    if settings.is_live:
        from faultline.adapters.postgres import PostgresRepository
        from faultline.adapters.redis_streams import RedisBus, RedisEventPublisher

        bus: Bus = RedisBus(settings.redis_url)
        repository: Repository = PostgresRepository(settings.postgres_dsn)
        publisher: EventPublisher = RedisEventPublisher(settings.redis_url)
    else:
        bus = InMemoryBus()
        repository = InMemoryRepository()
        publisher = InMemoryEventPublisher()
    return Container(
        settings=settings,
        bus=bus,
        repository=repository,
        publisher=publisher,
        signer=TokenSigner(settings.gateway_signing_key),
    )


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_principal(
    x_tenant_id: Annotated[str | None, Header()] = None,
    x_roles: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Development shim for OIDC.

    Production validates a JWT from Keycloak here and reads tenant and roles from
    verified claims. Everything downstream already takes a `Principal`, so that
    swap touches this function only. The tenant is never read from a request body
    or a query parameter -- that is what makes the repository's tenant filter
    impossible to widen from outside.
    """
    if not x_tenant_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing tenant")
    roles = frozenset(
        Role(r.strip()) for r in (x_roles or "viewer").split(",") if r.strip() in set(Role)
    )
    subject = (authorization or "Bearer dev").removeprefix("Bearer ").strip() or "dev"
    return Principal(
        subject=subject, tenant_id=x_tenant_id, roles=roles or frozenset({Role.VIEWER})
    )


ContainerDep = Annotated[Container, Depends(get_container)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
