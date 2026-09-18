"""The evidence ledger.

State holds this append-only list instead of a chat transcript, which is why
token use stays roughly flat as an investigation gets longer: each node rebuilds
its prompt from the ledger rather than replaying the conversation.

Every entry records what was asked, of which time range, and a hash of the exact
bytes the summary was derived from, so a claim can always be traced back.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

from faultline.core.schemas import Evidence, EvidenceId, EvidenceKind


class EvidenceLedger:
    """A read-side view over the evidence list carried in graph state.

    Deliberately not the storage itself: LangGraph owns the list (via an `operator.add`
    reducer, so parallel check nodes can append concurrently) and this wraps it.
    """

    def __init__(self, entries: Sequence[Evidence] | None = None) -> None:
        self._entries: list[Evidence] = list(entries or [])
        self._by_id: dict[EvidenceId, Evidence] = {e.id: e for e in self._entries}

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Evidence]:
        return iter(self._entries)

    def get(self, evidence_id: EvidenceId) -> Evidence | None:
        return self._by_id.get(evidence_id)

    def exists(self, evidence_id: EvidenceId) -> bool:
        return evidence_id in self._by_id

    def missing(self, evidence_ids: Iterable[EvidenceId]) -> list[EvidenceId]:
        """Which of these ids are not in the ledger. The citation-integrity check."""
        return [eid for eid in evidence_ids if eid not in self._by_id]

    def of_kind(self, kind: EvidenceKind) -> list[Evidence]:
        return [e for e in self._entries if e.kind == kind]

    def kinds(self, evidence_ids: Iterable[EvidenceId] | None = None) -> set[EvidenceKind]:
        """Distinct evidence kinds, used by the two-independent-sources conclude rule."""
        if evidence_ids is None:
            return {e.kind for e in self._entries}
        return {self._by_id[eid].kind for eid in evidence_ids if eid in self._by_id}

    def independent_support(self, evidence_ids: Iterable[EvidenceId]) -> bool:
        """True when the cited evidence spans at least two different kinds.

        A metric change point plus a deploy correlation counts. Three log queries
        does not, because they can all be downstream of the same wrong idea.
        """
        return len(self.kinds(evidence_ids)) >= 2

    def flagged(self) -> list[Evidence]:
        """Entries whose source content tripped an injection heuristic."""
        return [e for e in self._entries if e.injection_flagged]

    def digest(self, limit: int | None = None) -> str:
        """Compact rendering for prompts: one line per entry, newest last."""
        entries = self._entries if limit is None else self._entries[-limit:]
        return "\n".join(f"[{e.id}] ({e.kind}) {e.tool}({e.query}) -> {e.summary}" for e in entries)
