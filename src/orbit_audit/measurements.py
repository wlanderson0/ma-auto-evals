"""What each branch leaves for the scorers: authorship, generation counts, final requests.

The runner records every branch as it ends and writes the result to the sample's
store in ``_adopt`` (``MEASUREMENTS_KEY``), so abandoned branches survive. See
``docs/superpowers/specs/2026-10-20-inspect-native-measurements-design.md``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from inspect_ai.event import Event

from orbit_audit.timeline import AUDITOR_SPAN

MEASUREMENTS_KEY = "orbit_audit:measurements"
MEASUREMENTS_VERSION = 1

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

    def around(span_id: str | None):
        while span_id in spans:
            span = spans[span_id]
            yield span
            span_id = span.parent_id

    calls = []
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
