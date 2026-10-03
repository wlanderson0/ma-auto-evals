"""Append-only audit evidence: what the auditor did, and who authored what.

Orbit and Inspect already record what agents and tools did (``ModelEvent``,
``ToolEvent``, ``orbit.communication`` events). This module adds only the
intervention index the spec asks for (§5.1): each record links the actor,
boundary, affected agents, original and replacement content, and outcome.
Rejected operations are recorded too, so a failed attempt is never silent.

Records are mirrored into the Inspect transcript as ``orbit_audit_intervention``
info events, so a saved log carries them without this process.

Authorship labels (``docs/auditor-tests.md`` P1):

- ``agent:<id>``: autonomous output of agent ``<id>``'s own model.
- ``auditor-as:<id>``: written or edited by the auditor in agent ``<id>``'s seat.
- ``auditor``: auditor-staged input that does not claim to come from an agent.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

from inspect_ai.log import transcript
from inspect_ai.model import ChatMessage
from pydantic import BaseModel, ConfigDict, Field

from orbit_audit.boundaries import Boundary

EVIDENCE_VERSION = 1
AUTHOR_KEY = "orbit_audit_author"
"""ChatMessage metadata key holding the authorship label of auditor-touched content."""

Outcome = Literal["applied", "rejected"]


def agent_author(agent: str) -> str:
    return f"agent:{agent}"


def auditor_as(agent: str) -> str:
    return f"auditor-as:{agent}"


AUDITOR = "auditor"


def author_of(message: ChatMessage) -> str | None:
    """Authorship label recorded on a message by the auditor, if any.

    Untagged assistant messages are autonomous agent output; untagged
    user/tool/system messages come from the scenario, tools or Orbit itself.
    """
    return (message.metadata or {}).get(AUTHOR_KEY)


def tag(message: ChatMessage, author: str) -> ChatMessage:
    """Return a copy of ``message`` labelled with ``author``."""
    metadata = dict(message.metadata or {})
    metadata[AUTHOR_KEY] = author
    return message.model_copy(update={"metadata": metadata}, deep=True)


class InterventionRecord(BaseModel):
    """One attempted control operation and its outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = EVIDENCE_VERSION
    sequence: int
    op: str
    op_version: int
    boundary: Boundary
    actor: str
    author: str | None = None
    """Authorship label given to content this operation produced, if any."""
    agents: tuple[str, ...] = ()
    turn: int | None = None
    revision: int
    """Audit state revision the operation was validated against."""
    outcome: Outcome
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    original: Any = None
    replacement: Any = None
    orbit: dict[str, Any] = Field(default_factory=dict)
    """Orbit identifiers (session, invocation) linking to native evidence."""
    details: dict[str, Any] = Field(default_factory=dict)
    """Operation-specific evidence, e.g. topology violations of an override."""


class EvidenceLedger:
    """Append-only, ordered record of attempted interventions for one branch."""

    def __init__(self, *, emit: bool = True) -> None:
        self._records: list[InterventionRecord] = []
        self._emit = emit

    def __iter__(self) -> Iterator[InterventionRecord]:
        return iter(tuple(self._records))

    def __len__(self) -> int:
        return len(self._records)

    @property
    def records(self) -> tuple[InterventionRecord, ...]:
        return tuple(self._records)

    def applied(self) -> tuple[InterventionRecord, ...]:
        return tuple(r for r in self._records if r.outcome == "applied")

    def rejected(self) -> tuple[InterventionRecord, ...]:
        return tuple(r for r in self._records if r.outcome == "rejected")

    def record(self, **fields: Any) -> InterventionRecord:
        record = InterventionRecord(sequence=len(self._records), **fields)
        self._records.append(record)
        if self._emit:
            transcript().info(
                {"event": "orbit_audit_intervention", **record.model_dump(mode="json")},
                source="orbit_audit",
            )
        return record
