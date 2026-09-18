"""Gateway policy: capability tokens, redaction and injection flagging.

The security model in one sentence: the worker never holds a credential, and the
gateway never performs a write without a token that names one incident, one
action and one target.

A capability token authorizes reads. An approval token authorizes exactly one
write, and the API mints it only after a human approves that specific action.
"""

from __future__ import annotations

import base64
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

# Patterns that must never reach a model prompt. Applied to every tool result
# before it is summarized, not after.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b(?:sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b"), "<secret>"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}\b", re.I), "Bearer <token>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "<jwt>"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key)\s*[=:]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "<card>"),
]

# Heuristics for text that is trying to talk to the model rather than describe a
# system. Log lines and span attributes are attacker-controllable, so anything
# that trips these is quarantined: kept as evidence, marked, never trusted.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+instructions"),
    re.compile(r"(?i)\b(system|assistant|developer)\s*(prompt|message)\s*[:>]"),
    re.compile(r"(?i)you\s+are\s+(now\s+)?(a|an)\s+\w+"),
    re.compile(r"(?i)\b(disregard|override)\b.{0,32}\b(rules|policy|guardrails)\b"),
    re.compile(r"(?i)\b(run|execute|exec|curl|kubectl|rm\s+-rf)\b.{0,40}\b(immediately|now)\b"),
    re.compile(r"(?i)approved\s+by\s+(the\s+)?(admin|operator|on-?call|user)"),
    re.compile(r"(?i)\b(tool|function)_call\b|<\s*/?\s*(tool|function|system)\s*>"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def looks_like_injection(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INJECTION_PATTERNS)


class PolicyError(PermissionError):
    """Raised when a tool call is not authorized. Never retried."""


@dataclass(frozen=True, slots=True)
class Capability:
    """What one investigation is allowed to do."""

    tenant_id: str
    incident_id: str
    tools: tuple[str, ...]
    namespaces: tuple[str, ...]
    expires_at: datetime

    def allows(self, tool: str, namespace: str | None = None) -> bool:
        if datetime.now(UTC) >= self.expires_at:
            return False
        if "*" not in self.tools and tool not in self.tools:
            return False
        return not (namespace and "*" not in self.namespaces and namespace not in self.namespaces)


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """Authorization for exactly one write, bound to incident, action and target."""

    tenant_id: str
    incident_id: str
    action_id: str
    tool: str
    target: str
    approver: str
    expires_at: datetime


class TokenSigner:
    """Short-lived signed tokens. HMAC keeps the skeleton dependency-free; the
    production path swaps this for the platform's workload identity."""

    def __init__(self, key: str) -> None:
        self._key = key.encode("utf-8")

    def _sign(self, body: bytes) -> str:
        payload = base64.urlsafe_b64encode(body).decode().rstrip("=")
        mac = hmac.new(self._key, payload.encode(), sha256).hexdigest()[:32]
        return f"{payload}.{mac}"

    def _verify(self, token: str) -> dict[str, object]:
        try:
            payload, mac = token.rsplit(".", 1)
        except ValueError as exc:
            raise PolicyError("malformed token") from exc
        expected = hmac.new(self._key, payload.encode(), sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, expected):
            raise PolicyError("bad token signature")
        padded = payload + "=" * (-len(payload) % 4)
        data: dict[str, object] = json.loads(base64.urlsafe_b64decode(padded))
        expires = datetime.fromisoformat(str(data["exp"]))
        if datetime.now(UTC) >= expires:
            raise PolicyError("token expired")
        return data

    def mint_capability(
        self,
        tenant_id: str,
        incident_id: str,
        tools: tuple[str, ...] = ("*",),
        namespaces: tuple[str, ...] = ("*",),
        ttl: timedelta = timedelta(minutes=30),
    ) -> str:
        return self._sign(
            json.dumps(
                {
                    "typ": "cap",
                    "tenant": tenant_id,
                    "incident": incident_id,
                    "tools": list(tools),
                    "namespaces": list(namespaces),
                    "exp": (datetime.now(UTC) + ttl).isoformat(),
                }
            ).encode()
        )

    def read_capability(self, token: str) -> Capability:
        data = self._verify(token)
        if data.get("typ") != "cap":
            raise PolicyError("not a capability token")
        return Capability(
            tenant_id=str(data["tenant"]),
            incident_id=str(data["incident"]),
            tools=tuple(data["tools"]),  # type: ignore[arg-type]
            namespaces=tuple(data["namespaces"]),  # type: ignore[arg-type]
            expires_at=datetime.fromisoformat(str(data["exp"])),
        )

    def mint_approval(
        self,
        tenant_id: str,
        incident_id: str,
        action_id: str,
        tool: str,
        target: str,
        approver: str,
        ttl: timedelta = timedelta(minutes=10),
    ) -> str:
        return self._sign(
            json.dumps(
                {
                    "typ": "approval",
                    "tenant": tenant_id,
                    "incident": incident_id,
                    "action": action_id,
                    "tool": tool,
                    "target": target,
                    "approver": approver,
                    "exp": (datetime.now(UTC) + ttl).isoformat(),
                }
            ).encode()
        )

    def read_approval(self, token: str) -> ApprovalGrant:
        data = self._verify(token)
        if data.get("typ") != "approval":
            raise PolicyError("not an approval token")
        return ApprovalGrant(
            tenant_id=str(data["tenant"]),
            incident_id=str(data["incident"]),
            action_id=str(data["action"]),
            tool=str(data["tool"]),
            target=str(data["target"]),
            approver=str(data["approver"]),
            expires_at=datetime.fromisoformat(str(data["exp"])),
        )
