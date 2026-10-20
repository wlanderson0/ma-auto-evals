"""What each branch leaves for the scorers: authorship, generation counts, final requests.

The runner records every branch as it ends and writes the result to the sample's
store in ``_adopt`` (``MEASUREMENTS_KEY``), so abandoned branches survive. See
``docs/superpowers/specs/2026-10-20-inspect-native-measurements-design.md``.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from inspect_ai.event import Event, SpanBeginEvent

from orbit_audit.timeline import AUDITOR_SPAN

if TYPE_CHECKING:
    from orbit_audit.controller import AuditController

MEASUREMENTS_KEY = "orbit_audit:measurements"
MEASUREMENTS_VERSION = 1

logger = logging.getLogger(__name__)

Entry = tuple[str, str]
"""A ``ModelEvent`` uuid and the branch whose run produced it."""


@dataclass(frozen=True)
class Gen:
    """One traced generation: ``exposed`` means a model ran, ``replayed`` means the
    parent's recording was re-executed instead."""

    agent: str
    index: int
    exposed: bool
    replayed: bool


@dataclass(frozen=True)
class ModelCall:
    """A target agent's model call found in the transcript."""

    uuid: str
    agent: str
    branch: str


@dataclass(frozen=True)
class Alignment:
    lists: dict[str, list[Entry | None]]
    """Per agent, one entry per generation, in order; ``None`` where no model event exists."""
    problems: dict[str, str]
    """Per agent, why its live entries could not be matched."""


def target_model_calls(events: Sequence[Event]) -> list[ModelCall]:
    """Target-model calls, by the agent and branch spans around them.

    The auditor's own model calls nest under the agent's span too (an
    ``orbit_audit.auditor`` span), so any call inside one is excluded.
    """
    spans = {e.id: e for e in events if e.event == "span_begin"}

    def around(span_id: str | None) -> Iterator[SpanBeginEvent]:
        while span_id in spans:
            span = spans[span_id]
            yield span
            span_id = span.parent_id

    calls: list[ModelCall] = []
    for event in events:
        if event.event != "model" or event.pending or event.error or event.uuid is None:
            continue
        enclosing = list(around(event.span_id))
        if any(span.type == AUDITOR_SPAN for span in enclosing):
            continue
        agent = next((s.name for s in enclosing if s.type == "agent"), None)
        branch = next((s.name for s in enclosing if s.type == "branch"), None)
        if agent is not None and branch is not None:
            calls.append(ModelCall(event.uuid, agent, branch))
    return calls


def align_requests(
    *, branch: str, generations: Sequence[Gen], calls: Sequence[ModelCall],
    parent: Mapping[str, list[Entry | None]] | None,
) -> Alignment:
    """Match each agent's generations in ``branch`` to model events.

    A replayed generation takes the parent's entry at the same index; one the
    auditor wrote (no model ran) has none; a live one takes the next model event
    of that agent in this branch. If the live generations and the events differ in
    number, the live entries are left unknown and the problem is recorded.
    """
    lists: dict[str, list[Entry | None]] = {}
    problems: dict[str, str] = {}
    for agent in sorted({g.agent for g in generations}):
        mine = sorted((g for g in generations if g.agent == agent), key=lambda g: g.index)
        found = [c for c in calls if c.agent == agent and c.branch == branch]
        wanted = sum(g.exposed and not g.replayed for g in mine)
        if wanted != len(found):
            problems[agent] = (f"{wanted} live generation(s) but {len(found)} model event(s) "
                               f"in {branch}")
            found = []
        events = iter(found)
        inherited = (parent or {}).get(agent, [])
        entries: list[Entry | None] = []
        for gen in mine:
            if gen.replayed:
                entries.append(inherited[gen.index] if gen.index < len(inherited) else None)
            elif not gen.exposed:
                entries.append(None)
            else:
                call = next(events, None)
                entries.append((call.uuid, branch) if call else None)
        lists[agent] = entries
    return Alignment(lists, problems)


def stored_requests(alignment: Alignment) -> dict[str, dict[str, Any]]:
    """Each agent's final request in the form written to the store."""
    stored: dict[str, dict[str, Any]] = {}
    for agent, entries in alignment.lists.items():
        last = entries[-1] if entries else None
        if last is not None:
            stored[agent] = {"event": last[0], "via": last[1], "why": ""}
        else:
            stored[agent] = {
                "event": None, "via": None,
                "why": alignment.problems.get(agent)
                or "the agent's last generation has no model event of its own",
            }
    return stored


class MeasurementRecorder:
    """Collects one record per branch, in memory, until ``_adopt`` writes them."""

    def __init__(self) -> None:
        self._lists: dict[str, dict[str, list[Entry | None]]] = {}
        self._branches: dict[str, dict[str, Any]] = {}

    def record(self, controller: AuditController, parent: str | None,
               events: Sequence[Event]) -> None:
        """Record ``controller``'s branch, which has just ended. ``parent`` finished earlier.

        Measuring must never fail the audit: a problem is noted in the branch's
        record (``record_error``) and the scorers read it as unknown.
        """
        branch = controller.branch
        try:
            self._record(controller, parent, events)
        except Exception as error:
            logger.warning("orbit_audit: could not record %s's measurements: %s",
                           branch, error, exc_info=True)
            self._lists[branch] = {}
            self._branches[branch] = {
                "authors": None, "generations": {}, "requests": {},
                "record_error": f"{type(error).__name__}: {error}"}

    def _record(self, controller: AuditController, parent: str | None,
                events: Sequence[Event]) -> None:
        branch = controller.branch
        generations = [Gen(t.agent, t.index, t.exposed, t.replayed)
                       for t in controller.generations]
        alignment = align_requests(
            branch=branch, generations=generations, calls=target_model_calls(events),
            parent=self._lists.get(parent) if parent is not None else None)
        self._lists[branch] = alignment.lists
        record: dict[str, Any] = {
            "authors": None,
            "generations": dict(Counter(g.agent for g in generations)),
            "requests": stored_requests(alignment),
        }
        if controller.runtime is not None:
            try:
                record["authors"] = controller.authors()
            except Exception as error:  # a view problem, not a run problem
                logger.warning("orbit_audit: could not label %s's messages: %s", branch, error,
                               exc_info=True)
                record["authors_error"] = f"{type(error).__name__}: {error}"
        self._branches[branch] = record

    def payload(self) -> dict[str, Any]:
        return {"version": MEASUREMENTS_VERSION, "branches": dict(self._branches)}
