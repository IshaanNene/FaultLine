"""Alerts, alert groups and correlation.

The correlator is deliberately the only thing that runs before an LLM does. An
alert storm is thousands of alerts describing one failure; collapsing them here
is the single largest cost lever in the system, and it is pure Python.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from faultline.ids import fingerprint, new_id

# Labels that identify "the same alert firing again" rather than a new problem.
# Deliberately excludes pod/instance: one crashlooping deployment is one alert,
# not forty.
IDENTITY_LABELS = ("alertname", "namespace", "service", "severity")


class Severity(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"

    @property
    def rank(self) -> int:
        return {"P1": 0, "P2": 1, "P3": 2, "P4": 3}[self.value]

    @classmethod
    def from_label(cls, value: str | None) -> Severity:
        match (value or "").lower():
            case "critical" | "page" | "p1":
                return cls.P1
            case "high" | "error" | "p2":
                return cls.P2
            case "warning" | "warn" | "p3":
                return cls.P3
            case _:
                return cls.P4


class Alert(BaseModel):
    """One Alertmanager alert, normalized."""

    fingerprint: str = ""
    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ends_at: datetime | None = None
    generator_url: str | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    def model_post_init(self, _context: object) -> None:
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", fingerprint(self.labels, IDENTITY_LABELS))

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "UnknownAlert")

    @property
    def service(self) -> str | None:
        return self.labels.get("service") or self.labels.get("job")

    @property
    def namespace(self) -> str | None:
        return self.labels.get("namespace")

    @property
    def severity(self) -> Severity:
        return Severity.from_label(self.labels.get("severity"))

    @property
    def is_firing(self) -> bool:
        return self.status == "firing"


class AlertGroup(BaseModel):
    """A set of alerts the correlator believes describe one failure."""

    group_key: str
    tenant_id: str
    alerts: list[Alert] = Field(default_factory=list)
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def severity(self) -> Severity:
        return min((a.severity for a in self.alerts), key=lambda s: s.rank, default=Severity.P4)

    @property
    def services(self) -> list[str]:
        seen = {a.service for a in self.alerts if a.service}
        return sorted(seen)

    @property
    def first_firing_at(self) -> datetime:
        firing = [a.starts_at for a in self.alerts if a.is_firing]
        return min(firing) if firing else self.received_at

    def window(self, lookback: timedelta = timedelta(minutes=30)) -> TimeWindow:
        """The interval an investigation should look at: before the first alert, to now."""
        start = self.first_firing_at - lookback
        return TimeWindow(start=start, end=datetime.now(UTC))


class TimeWindow(BaseModel):
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def clamp(self, max_duration: timedelta) -> TimeWindow:
        """Tools cap their own windows; an agent asking for six months of logs is a bug."""
        if self.duration <= max_duration:
            return self
        return TimeWindow(start=self.end - max_duration, end=self.end)


def correlate(
    alerts: list[Alert],
    tenant_id: str,
    window_seconds: int = 120,
) -> list[AlertGroup]:
    """Collapse alerts into groups.

    Two alerts join the same group when they share a namespace and their start
    times fall inside the correlation window. This is intentionally simple and
    topology-blind: the topology-aware pass belongs in the prefetch node, where
    the trace-derived service graph is already loaded.
    """
    firing = sorted((a for a in alerts if a.is_firing), key=lambda a: a.starts_at)
    if not firing:
        return []

    groups: list[list[Alert]] = []
    for alert in firing:
        for group in groups:
            same_scope = group[0].namespace == alert.namespace
            within = abs((alert.starts_at - group[0].starts_at).total_seconds()) <= window_seconds
            if same_scope and within:
                group.append(alert)
                break
        else:
            groups.append([alert])

    result = []
    for group in groups:
        # The group key is deterministic, so a redelivered webhook maps to the
        # same key and the repository's unique constraint does the deduping.
        key = fingerprint(
            {
                "namespace": group[0].namespace or "-",
                "tenant": tenant_id,
                "members": ",".join(sorted(a.fingerprint for a in group)),
            }
        )
        result.append(AlertGroup(group_key=key, tenant_id=tenant_id, alerts=group))
    return result


def make_incident_id() -> str:
    return new_id("inc")
