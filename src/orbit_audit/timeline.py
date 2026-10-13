"""The ``auditor`` timeline: what the auditor saw, said and queued at each wake, by branch.

Built once, after a sample's branches finish, from the transcript's events alone
(``docs/superpowers/specs/2026-10-13-auditor-timeline-design.md``). Inspect's
default timeline dissolves ``type="branch"`` spans and has no view of the
auditor, so the viewer gets this one instead, next to a ``default`` timeline.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from inspect_ai.event import (
    Event,
    EventTreeSpan,
    Timeline,
    TimelineEvent,
    TimelineSpan,
    event_tree,
)

AUDITOR_SPAN = "orbit_audit.auditor"
"""Span type of both the Level 1 ``auditor`` span and the Level 0 ``auditor-seat`` span."""
TURN_EVENTS = ("orbit_audit_auditor_turn", "orbit_audit_seat_turn")


@dataclass(frozen=True)
class _Wake:
    span: EventTreeSpan
    branch: str
    turn: Event | None
    """The turn info event emitted right after the span, if the wake finished."""


def _is_info(node: Any, *kinds: str) -> bool:
    """Whether ``node`` is an ``orbit_audit`` info event, optionally of one of ``kinds``."""
    return (
        not isinstance(node, EventTreeSpan)
        and node.event == "info"
        and getattr(node, "source", None) == "orbit_audit"
        and isinstance(node.data, dict)
        and (not kinds or node.data.get("event") in kinds)
    )


def _turn_after(nodes: Sequence[Any], index: int) -> Event | None:
    """The turn event of the auditor span at ``nodes[index]``: the next one among its siblings."""
    for node in nodes[index + 1 :]:
        if isinstance(node, EventTreeSpan):
            if node.type == AUDITOR_SPAN:
                return None  # the next wake's turn event is not this wake's
            continue
        if _is_info(node, *TURN_EVENTS):
            return node
    return None


def _scan(
    nodes: Sequence[Any], branch: str, wakes: list[_Wake], infos: list[Event]
) -> None:
    for index, node in enumerate(nodes):
        if isinstance(node, EventTreeSpan):
            if node.type == AUDITOR_SPAN:
                wakes.append(_Wake(node, branch, _turn_after(nodes, index)))
            _scan(node.children, node.name if node.type == "branch" else branch, wakes, infos)
        elif _is_info(node):
            infos.append(node)


def _events_in(span: EventTreeSpan) -> Iterator[Event]:
    for child in span.children:
        if isinstance(child, EventTreeSpan):
            yield from _events_in(child)
        else:
            yield child


def _wake_name(wake: _Wake, interventions: Sequence[Event]) -> str:
    if wake.turn is None:
        return "auditor wake — no turn event"
    turn = wake.turn.data
    if "seat" in turn:
        head = f"r{turn['revision']} · {turn['seat']} · generation (seat)"
    else:
        agent = [turn["agent"]] if turn.get("agent") else []
        head = " · ".join([f"r{turn['revision']}", *agent, turn["boundary"]])
    ops = ", ".join(f"{e.data['op']} {e.data['outcome']}" for e in interventions) or "no ops"
    return f"{head} — {ops}"


def _lane_name(branch: str, node: dict[str, Any] | None) -> str:
    if node and node.get("parent"):
        return f"{branch} (forked from {node['parent']} @ r{node['fork_revision']})"
    return branch


def build_auditor_timeline(events: Sequence[Event]) -> Timeline | None:
    """The ``auditor`` timeline for one sample's events, or ``None`` if the auditor never woke."""
    wakes: list[_Wake] = []
    infos: list[Event] = []
    _scan(event_tree(events), "?", wakes, infos)
    if not wakes:
        return None

    live = [e for e in infos
            if e.data["event"] == "orbit_audit_intervention" and not e.data["replayed"]]
    started = {e.data["id"]: e.data for e in infos
               if e.data["event"] == "orbit_audit_branch" and e.data["action"] == "started"}
    lanes: dict[str, list[tuple[datetime, TimelineEvent | TimelineSpan]]] = {}
    claimed: set[int] = set()

    for wake in wakes:
        mine = [] if wake.turn is None else [
            e for e in live
            if (e.data["branch"], e.data["revision"])
            == (wake.turn.data["branch"], wake.turn.data["revision"])
        ]
        claimed.update(id(e) for e in mine)
        content = [TimelineEvent(event=e) for e in _events_in(wake.span)]
        content += [TimelineEvent(event=e) for e in ([wake.turn] if wake.turn else [])]
        content += [TimelineEvent(event=e) for e in mine]
        if not content:
            continue  # an auditor span that recorded nothing has nothing to show
        span = TimelineSpan(id=wake.span.id, name=_wake_name(wake, mine),
                            span_type="agent", content=content)
        lanes.setdefault(wake.branch, []).append((wake.span.begin.timestamp, span))

    for event in infos:  # everything else orbit_audit wrote, in the lane it belongs to
        kind = event.data["event"]
        if kind == "orbit_audit_branch":
            lanes.setdefault(event.data["id"], []).append(
                (event.timestamp, TimelineEvent(event=event)))
        elif kind == "orbit_audit_intervention" and id(event) not in claimed \
                and not event.data["replayed"]:
            lanes.setdefault(event.data["branch"], []).append(
                (event.timestamp, TimelineEvent(event=event)))

    order = [*started, *sorted(set(lanes) - set(started))]
    lane_spans = [
        TimelineSpan(
            id=branch, name=_lane_name(branch, started.get(branch)), span_type="agent",
            content=[item for _, item in sorted(lanes[branch], key=lambda pair: pair[0])],
        )
        for branch in order if branch in lanes
    ]
    return Timeline(
        name="auditor",
        description="What the auditor saw, said and queued at each wake, by branch",
        root=TimelineSpan(id="auditor", name="auditor", span_type="agent", content=lane_spans),
    )
