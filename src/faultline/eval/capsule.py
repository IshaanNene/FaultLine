"""Incident capsules: self-contained, replayable incidents with known answers.

A capsule is everything an investigation can observe about one incident -- its
alert group, topology, metrics, log templates, change events and Kubernetes state
-- plus the ground truth of what actually broke. The gateway serves the
observable half; only the scorer reads the answer.

**Capsules are time-invariant.** Timestamps are stored relative to the incident
start, and rebased to the moment of replay on load. That matters more than it
sounds: several of Faultline's own rules are temporal -- the bystander filter
demotes anomalies that began before the incident, the verifier rejects a cause
that postdates its effect -- so a capsule with month-old absolute timestamps
would exercise different code than the one that was recorded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from faultline.core.alerts import Alert
from faultline.core.schemas import FaultClass
from faultline.gateway.backends.scenario import (
    SCENARIOS,
    AlertSpec,
    ChangeEvent,
    GroundTruth,
    LogTemplate,
    MetricSeries,
    Scenario,
    WorkloadState,
)

CAPSULE_FORMAT_VERSION = 1


@dataclass(slots=True)
class Capsule:
    name: str
    scenario: Scenario

    @property
    def truth(self) -> GroundTruth:
        return self.scenario.ground_truth

    def alerts(self) -> list[Alert]:
        """The alert group as Alertmanager would have delivered it."""
        now = datetime.now(UTC)
        return [
            Alert(
                status=spec.status,
                labels={
                    "alertname": spec.alertname,
                    "service": spec.service,
                    "severity": spec.severity,
                    "namespace": self.scenario.namespace,
                },
                annotations={"summary": spec.summary} if spec.summary else {},
                starts_at=now - timedelta(minutes=spec.minutes_ago),
            )
            for spec in self.scenario.alerts
        ]

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Offsets, not absolute times -- see the module docstring."""
        origin = self.scenario.incident_start

        def offset(moment: datetime | None) -> float | None:
            return None if moment is None else (moment - origin).total_seconds()

        return {
            "format_version": CAPSULE_FORMAT_VERSION,
            "name": self.name,
            "namespace": self.scenario.namespace,
            "alerts": [
                {
                    "alertname": a.alertname,
                    "service": a.service,
                    "severity": a.severity,
                    "status": a.status,
                    "minutes_ago": a.minutes_ago,
                    "summary": a.summary,
                }
                for a in self.scenario.alerts
            ],
            "topology": self.scenario.topology,
            "metrics": [
                {
                    "service": m.service,
                    "name": m.name,
                    "baseline": m.baseline,
                    "current": m.current,
                    "unit": m.unit,
                    "change_point_offset_s": offset(m.change_point),
                }
                for m in self.scenario.metrics
            ],
            "logs": [
                {
                    "service": t.service,
                    "level": t.level,
                    "template": t.template,
                    "count_window": t.count_window,
                    "count_baseline": t.count_baseline,
                    "example": t.example,
                }
                for t in self.scenario.logs
            ],
            "changes": [
                {
                    "offset_s": offset(c.at),
                    "kind": c.kind,
                    "service": c.service,
                    "description": c.description,
                    "detail": c.detail,
                }
                for c in self.scenario.changes
            ],
            "workloads": [
                {
                    "service": w.service,
                    "namespace": w.namespace,
                    "replicas_desired": w.replicas_desired,
                    "replicas_ready": w.replicas_ready,
                    "restarts": w.restarts,
                    "image": w.image,
                    "last_terminated_reason": w.last_terminated_reason,
                    "env_keys": w.env_keys,
                }
                for w in self.scenario.workloads
            ],
            "ground_truth": {
                "root_cause_service": self.truth.root_cause_service,
                "fault_class": self.truth.fault_class.value,
                "mechanism": self.truth.mechanism,
                "first_bad_at_offset_s": offset(self.truth.first_bad_at),
                "expect_abstention": self.truth.expect_abstention,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], now: datetime | None = None) -> Capsule:
        version = data.get("format_version")
        if version != CAPSULE_FORMAT_VERSION:
            raise ValueError(
                f"capsule format v{version} is not v{CAPSULE_FORMAT_VERSION}; "
                "migrate it rather than replaying it against different rules"
            )
        # Rebase: the incident starts now, everything else keeps its relative
        # distance from it.
        origin = now or datetime.now(UTC)

        def moment(offset: float | None) -> datetime | None:
            return None if offset is None else origin + timedelta(seconds=offset)

        truth = data["ground_truth"]
        scenario = Scenario(
            name=data["name"],
            namespace=data["namespace"],
            incident_start=origin,
            alerts=[AlertSpec(**a) for a in data["alerts"]],
            topology={k: list(v) for k, v in data["topology"].items()},
            metrics=[
                MetricSeries(
                    service=m["service"],
                    name=m["name"],
                    baseline=m["baseline"],
                    current=m["current"],
                    unit=m["unit"],
                    change_point=moment(m["change_point_offset_s"]),
                )
                for m in data["metrics"]
            ],
            logs=[LogTemplate(**t) for t in data["logs"]],
            changes=[
                ChangeEvent(
                    at=moment(c["offset_s"]) or origin,
                    kind=c["kind"],
                    service=c["service"],
                    description=c["description"],
                    detail=dict(c["detail"]),
                )
                for c in data["changes"]
            ],
            workloads=[WorkloadState(**w) for w in data["workloads"]],
            ground_truth=GroundTruth(
                root_cause_service=truth["root_cause_service"],
                fault_class=FaultClass(truth["fault_class"]),
                mechanism=truth["mechanism"],
                first_bad_at=moment(truth["first_bad_at_offset_s"]) or origin,
                expect_abstention=truth["expect_abstention"],
            ),
        )
        return cls(name=data["name"], scenario=scenario)

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        return path

    @classmethod
    def load(cls, path: Path, now: datetime | None = None) -> Capsule:
        return cls.from_dict(json.loads(path.read_text()), now=now)


def builtin(name: str) -> Capsule:
    if name not in SCENARIOS:
        raise KeyError(f"unknown capsule {name!r}; have: {', '.join(sorted(SCENARIOS))}")
    return Capsule(name=name, scenario=SCENARIOS[name]())


def builtins() -> list[Capsule]:
    """Every built-in capsule, in a stable order so runs are comparable."""
    return [builtin(name) for name in sorted(SCENARIOS)]
