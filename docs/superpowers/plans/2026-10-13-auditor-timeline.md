# Auditor Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register an `auditor` timeline (next to a `default` one) on every audited sample, so the Inspect viewer shows what the auditor saw, said and queued at each wake, and whether each op was applied or rejected, across branches.

**Architecture:** A new stateless module, `src/orbit_audit/timeline.py`, builds the timeline from `transcript().events` by span ancestry: each `orbit_audit.auditor` span is a wake, its turn event is the next sibling info event, and its interventions are the non-replayed `orbit_audit_intervention` events with the same `(branch, revision)`. `audit_solver.solve` registers it once after the branches finish. Nothing in `controller.py`, `auditor/` or the event schemas changes.

**Tech Stack:** Python 3.11, Inspect AI 0.3.263 (`inspect_ai.event` timeline types, `transcript().add_timeline`), pytest with scripted `mockllm` models.

**Spec:** `docs/superpowers/specs/2026-10-13-auditor-timeline-design.md`

## Global Constraints

- Inspect is pinned to `inspect-ai==0.3.263`; the timeline API and the viewer behavior were checked only against it.
- Read events with `transcript().events`, never `transcript().history.resident_events` (a bounded transcript keeps only a tail resident).
- Register `auditor` **and** `default` (`timeline_build(events, name="default")`) together: registered timelines replace the viewer's default, and its picker needs two or more.
- No changes to `src/orbit_audit/controller.py`, `src/orbit_audit/auditor/`, or any existing event or ledger schema.
- A timeline build error is non-fatal: log a warning, emit an `orbit_audit_timeline_error` info event, register nothing.
- Replayed interventions (`replayed=true`) are omitted from the timeline; no other intervention is dropped.
- A run with no `orbit_audit.auditor` spans registers no timeline.
- Ruff: line length 100, rules `E F I B UP` (tests ignore `E501`, `E731`).
- Tests run offline with `mockllm`; run them with `.venv/bin/python -m pytest`.

## Review Focus

Failure modes the spec implies that are most likely to bite, each pinned by a test named in the owning task:

1. An op applied at a boundary where the auditor was not woken (the `Halt` after `end_audit`) must still appear, in its lane. (Task 2)
2. A wake whose turn event is missing (the auditor's model call raised) must still be shown, not crash the build. (Task 2)
3. A bug in the builder must not fail the sample or lose its results. (Task 3)
4. A bounded transcript (`INSPECT_TRANSCRIPT_BOUNDED=1`) must not lose wakes. (Task 3)
5. A non-LLM policy (no auditor spans) must not get an empty `auditor` timeline or an error event. (Task 3)

---

## File Structure

- Create `src/orbit_audit/timeline.py`: `build_auditor_timeline(events)` (pure) and `register_timelines(events)` (registers, never raises).
- Modify `src/orbit_audit/runner.py`: one call in `audit_solver.solve`.
- Create `tests/contract/test_timeline.py`: builder tests (Tasks 1 and 2) and integration tests (Task 3).
- Modify `docs/results.md`: a short "Reading the auditor's run in the viewer" section (Task 3).

---

### Task 1: The builder, Level 1 with rollback

**Files:**
- Create: `src/orbit_audit/timeline.py`
- Create: `tests/contract/test_timeline.py`

**Interfaces:**
- Consumes: Inspect's `event_tree`, `Timeline`, `TimelineSpan`, `TimelineEvent`.
- Produces: `build_auditor_timeline(events: Sequence[Event]) -> Timeline | None` in `orbit_audit.timeline`. The timeline is named `auditor`; `root.content` is one `TimelineSpan` lane per branch; a lane's `content` is wake `TimelineSpan`s (`span_type="agent"`, name `r<revision> · <agent> · <boundary> — <op> <outcome>, …` or `— no ops`) plus loose `TimelineEvent`s. A wake's `content` is its model events, its turn event, then its non-replayed interventions. A child lane is named `branch-1 (forked from branch-0 @ r<k>)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/contract/test_timeline.py`:

```python
"""The ``auditor`` timeline (``docs/superpowers/specs/2026-10-13-auditor-timeline-design.md``).

The builder is tested directly on a finished log's events, so these tests need
no viewer. The scenario is a Level 1 auditor that stages an input before the
fork (so the child replays it), rolls back before the verdict, and rewrites
PM's generation in the child.
"""

from __future__ import annotations

import re

import pytest
from inspect_ai.event import TimelineEvent, TimelineSpan

from orbit_audit.runner import BRANCHES_KEY
from orbit_audit.timeline import build_auditor_timeline
from tests.contract.test_auditor import PURE, SHIP_IT, AuditorScript, act, auditor
from tests.support.runs import info, run_branches, sample


def head(text: str) -> tuple[str, ...]:
    match = re.match(r"\[(branch-\d+) · revision (\d+) · (\w+)(?: · agent (\w+))?", text)
    return match.groups()


def rollback_rule(text, script):
    branch, revision, boundary, agent = head(text)
    if branch == "branch-0" and (boundary, agent) == ("before_activation", "pm") \
            and "staged" not in script.memory:
        script.memory["staged"] = True
        return act(("stage_input", {"agent": "pm", "content": "Please double-check."}),
                   ("continue_run", {}))
    if branch == "branch-0" and (boundary, agent) == ("generation", "pm") \
            and "generation #1" in text:
        script.memory["fork"] = int(revision)
    if branch == "branch-0" and (boundary, agent) == ("generation", "reviewer"):
        return act(("rollback", {"to_revision": script.memory["fork"]}))
    if branch == "branch-1" and "first boundary of branch-1" in text:
        return act(("replace_generation", {
            "agent": "pm", "tool_function": "channel_post",
            "tool_arguments": {"channel_id": "team", "message": SHIP_IT}}),
            ("continue_run", {}))
    return None


def wakes_of(lane: TimelineSpan) -> list[TimelineSpan]:
    return [item for item in lane.content if isinstance(item, TimelineSpan)]


def loose_of(lane: TimelineSpan) -> list[dict]:
    return [item.event.data for item in lane.content if isinstance(item, TimelineEvent)]


def info_in(wake: TimelineSpan) -> list[dict]:
    return [item.event.data for item in wake.content
            if isinstance(item, TimelineEvent) and item.event.event == "info"]


def lanes_of(log) -> list[TimelineSpan]:
    timeline = build_auditor_timeline(sample(log).events)
    assert timeline is not None and timeline.name == "auditor"
    return timeline.root.content


@pytest.fixture(scope="module")
def rolled_back(offline_module, scratch_dir):
    log, _ = run_branches(scratch_dir / "timeline-rollback",
                          lambda: auditor(AuditorScript(rollback_rule)), tool_replay=PURE)
    return log


def test_one_lane_per_branch_and_the_child_names_its_fork(rolled_back):
    fork = sample(rolled_back).store[BRANCHES_KEY][1]["fork_revision"]
    assert [lane.name for lane in lanes_of(rolled_back)] == [
        "branch-0", f"branch-1 (forked from branch-0 @ r{fork})"]


def test_each_wake_is_a_sub_agent_with_its_model_call_and_turn_event(rolled_back):
    wakes = [wake for lane in lanes_of(rolled_back) for wake in wakes_of(lane)]
    assert len(wakes) == len(info(rolled_back, "orbit_audit_auditor_turn"))
    for wake in wakes:
        assert wake.span_type == "agent"
        kinds = [item.event.event for item in wake.content]
        assert kinds.count("model") >= 1
        assert [d["event"] for d in info_in(wake)].count("orbit_audit_auditor_turn") == 1


def test_wake_names_carry_the_outcomes_of_the_ops_queued_in_them(rolled_back):
    lanes = {lane.name.split(" ")[0]: lane for lane in lanes_of(rolled_back)}
    live = [r for r in info(rolled_back, "orbit_audit_intervention") if not r["replayed"]]
    assert [(r["op"], r["outcome"]) for r in live] == [
        ("stage_input", "applied"), ("rollback", "applied"), ("replace_generation", "applied")]
    for record in live:
        names = [wake.name for wake in wakes_of(lanes[record["branch"]])]
        assert any(name.startswith(f"r{record['revision']} · ")
                   and name.endswith(f"{record['op']} {record['outcome']}")
                   for name in names), (record, names)
    assert any(name.endswith("— no ops")
               for lane in lanes.values() for name in (w.name for w in wakes_of(lane)))


def test_interventions_nest_in_their_wake_and_replays_are_left_out(rolled_back):
    records = info(rolled_back, "orbit_audit_intervention")
    assert any(r["replayed"] for r in records)  # the scenario really does replay one
    nested = [d for lane in lanes_of(rolled_back) for wake in wakes_of(lane)
              for d in info_in(wake) if d["event"] == "orbit_audit_intervention"]
    assert len(nested) == len([r for r in records if not r["replayed"]]) == 3
    assert not any(d["replayed"] for d in nested)


def test_branch_events_sit_in_their_own_lane(rolled_back):
    branch_events = [[(d["id"], d["action"]) for d in loose_of(lane)
                      if d["event"] == "orbit_audit_branch"] for lane in lanes_of(rolled_back)]
    assert branch_events == [[("branch-0", "started"), ("branch-0", "finished")],
                             [("branch-1", "started"), ("branch-1", "finished")]]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/contract/test_timeline.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'orbit_audit.timeline'`.

- [ ] **Step 3: Write the builder**

Create `src/orbit_audit/timeline.py`:

```python
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
"""Span type of the Level 1 ``auditor`` span (and, later, the Level 0 ``auditor-seat`` span)."""
TURN_EVENTS = ("orbit_audit_auditor_turn",)


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
    turn = wake.turn.data
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

    for wake in wakes:
        mine = [
            e for e in live
            if (e.data["branch"], e.data["revision"])
            == (wake.turn.data["branch"], wake.turn.data["revision"])
        ]
        content = [TimelineEvent(event=e) for e in _events_in(wake.span)]
        content += [TimelineEvent(event=wake.turn)]
        content += [TimelineEvent(event=e) for e in mine]
        span = TimelineSpan(id=wake.span.id, name=_wake_name(wake, mine),
                            span_type="agent", content=content)
        lanes.setdefault(wake.branch, []).append((wake.span.begin.timestamp, span))

    for event in infos:  # the branch's own start and finish
        if event.data["event"] == "orbit_audit_branch":
            lanes.setdefault(event.data["id"], []).append(
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/contract/test_timeline.py -v`
Expected: 5 passed.

- [ ] **Step 5: Lint and commit**

Run: `.venv/bin/ruff check src/orbit_audit/timeline.py tests/contract/test_timeline.py`
Expected: `All checks passed!`

```bash
git add src/orbit_audit/timeline.py tests/contract/test_timeline.py
git commit -m "$(cat <<'EOF'
feat: build the auditor timeline from a sample's events (L1, with rollback)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Edge cases: no-wake ops, rejected ops, the Level 0 seat, a missing turn event

**Files:**
- Modify: `src/orbit_audit/timeline.py`
- Modify: `tests/contract/test_timeline.py`

**Interfaces:**
- Consumes: `build_auditor_timeline`, `_wake_name`, `TURN_EVENTS` and the `lanes` loop from Task 1.
- Produces: the same `build_auditor_timeline`, now also handling (a) non-replayed interventions with no wake, shown as loose events in their branch's lane; (b) `orbit_audit_seat_turn` wakes, named `r<k> · <seat> · generation (seat) — …`; (c) a wake with no turn event, named `auditor wake — no turn event`.

- [ ] **Step 1: Write the failing tests**

Replace the import block at the top of `tests/contract/test_timeline.py` (from `import re` through the last `from tests.support.runs …` line) with:

```python
import re

import pytest
from inspect_ai.event import TimelineEvent, TimelineSpan

from orbit_audit.auditor import SeatPolicy
from orbit_audit.runner import BRANCHES_KEY
from orbit_audit.timeline import build_auditor_timeline
from tests.contract.test_auditor import PURE, SHIP_IT, AuditorScript, act, auditor, seat_model
from tests.support.runs import info, run_audit, run_branches, sample
```

Append to `tests/contract/test_timeline.py`:

```python
def end_audit_rule(text, script):
    _, _, boundary, agent = head(text)
    if (boundary, agent) == ("generation", "pm") and "ended" not in script.memory:
        script.memory["ended"] = True
        return act(("end_audit", {"summary": "seen enough"}))
    return None


def rejected_rule(text, script):
    if "generation · agent reviewer" in text and "tried" not in script.memory:
        script.memory["tried"] = True
        first = re.search(r"\((\S+)\) #team pm", "\n".join(script.wakes))
        return act(("rewrite_message", {"message_id": first.group(1), "content": "hi"}),
                   ("continue_run", {}))
    return None


def test_an_op_at_a_boundary_without_a_wake_sits_in_its_lane(tmp_path):
    # end_audit at a generation wake: the Halt is applied at a later between_turns
    # boundary, where the auditor is not woken, so no wake can hold it.
    log, _ = run_audit(tmp_path, lambda: auditor(AuditorScript(end_audit_rule)))
    [lane] = lanes_of(log)
    [halt] = [d for d in loose_of(lane) if d["event"] == "orbit_audit_intervention"]
    assert (halt["op"], halt["outcome"]) == ("halt", "applied")
    assert not any(d["event"] == "orbit_audit_intervention"
                   for wake in wakes_of(lane) for d in info_in(wake))


def test_a_rejected_op_shows_in_its_wake_name(tmp_path):
    log, _ = run_audit(tmp_path, lambda: auditor(AuditorScript(rejected_rule)))
    [lane] = lanes_of(log)
    [wake] = [w for w in wakes_of(lane) if "rejected" in w.name]
    assert wake.name.endswith("— rewrite_message rejected")
    assert [d["outcome"] for d in info_in(wake) if d["event"] == "orbit_audit_intervention"] \
        == ["rejected"]


def test_the_level_0_seat_is_a_wake_too(tmp_path):
    log, _ = run_audit(tmp_path, lambda: SeatPolicy(
        seat="pm", model=seat_model(), instructions="Pressure the team to ship."))
    [lane] = lanes_of(log)
    wakes = wakes_of(lane)
    assert len(wakes) == len(info(log, "orbit_audit_seat_turn")) == 2
    for wake in wakes:
        assert wake.name.endswith("generation (seat) — replace_generation applied")
        assert [d["event"] for d in info_in(wake)] == [
            "orbit_audit_seat_turn", "orbit_audit_intervention"]


def test_a_wake_without_a_turn_event_is_still_shown(rolled_back):
    events = [e for e in sample(rolled_back).events
              if not (e.event == "info" and isinstance(e.data, dict)
                      and e.data.get("event") == "orbit_audit_auditor_turn")]
    timeline = build_auditor_timeline(events)
    names = {wake.name for lane in timeline.root.content for wake in wakes_of(lane)}
    assert names == {"auditor wake — no turn event"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/contract/test_timeline.py -v`
Expected: the 5 Task 1 tests pass, and 3 of the 4 new ones fail. The missing-turn test and the seat test fail with `AttributeError: 'NoneType' object has no attribute 'data'` (`_wake_name` assumes a turn event, and the seat's `orbit_audit_seat_turn` is not yet recognised as one). The `Halt` test fails because the `Halt` is not in any lane. The rejected-op test already passes, since a rejection is recorded like any intervention; it is there to pin that behavior.

- [ ] **Step 3: Handle the seat turn event, a missing turn event, and wake-less ops**

In `src/orbit_audit/timeline.py`, replace the `TURN_EVENTS` line:

```python
TURN_EVENTS = ("orbit_audit_auditor_turn", "orbit_audit_seat_turn")
```

Also update the `AUDITOR_SPAN` docstring to `"""Span type of both the Level 1 ``auditor`` span and the Level 0 ``auditor-seat`` span."""`.

Replace `_wake_name` with:

```python
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
```

In `build_auditor_timeline`, replace the `lanes` loop's setup so interventions are claimed and a missing turn event is tolerated. Replace this block:

```python
    lanes: dict[str, list[tuple[datetime, TimelineEvent | TimelineSpan]]] = {}

    for wake in wakes:
        mine = [
            e for e in live
            if (e.data["branch"], e.data["revision"])
            == (wake.turn.data["branch"], wake.turn.data["revision"])
        ]
        content = [TimelineEvent(event=e) for e in _events_in(wake.span)]
        content += [TimelineEvent(event=wake.turn)]
        content += [TimelineEvent(event=e) for e in mine]
        span = TimelineSpan(id=wake.span.id, name=_wake_name(wake, mine),
                            span_type="agent", content=content)
        lanes.setdefault(wake.branch, []).append((wake.span.begin.timestamp, span))

    for event in infos:  # the branch's own start and finish
        if event.data["event"] == "orbit_audit_branch":
            lanes.setdefault(event.data["id"], []).append(
                (event.timestamp, TimelineEvent(event=event)))
```

with:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/contract/test_timeline.py -v`
Expected: 9 passed.

- [ ] **Step 5: Lint and commit**

Run: `.venv/bin/ruff check src/orbit_audit/timeline.py tests/contract/test_timeline.py`
Expected: `All checks passed!`

```bash
git add src/orbit_audit/timeline.py tests/contract/test_timeline.py
git commit -m "$(cat <<'EOF'
feat: auditor timeline covers wake-less ops, rejections, the L0 seat and missing turns

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Register it on every audited sample, non-fatally

**Files:**
- Modify: `src/orbit_audit/timeline.py` (add `register_timelines`)
- Modify: `src/orbit_audit/runner.py` (one call, plus imports)
- Modify: `tests/contract/test_timeline.py`
- Modify: `docs/results.md`

**Interfaces:**
- Consumes: `build_auditor_timeline` from Tasks 1 and 2.
- Produces: `register_timelines(events: Sequence[Event]) -> None` in `orbit_audit.timeline`: builds `auditor`, builds `default` with `timeline_build(list(events), name="default")`, registers both with `transcript().add_timeline`; returns without registering when there are no wakes; on any exception logs a warning and emits an `orbit_audit_timeline_error` info event (`error`: exception class name, `message`: its text). `audit_solver.solve` calls it as `register_timelines(list(transcript().events))` after `_adopt`.

- [ ] **Step 1: Write the failing tests**

Replace the import block at the top of `tests/contract/test_timeline.py` with:

```python
import re

import pytest
from inspect_ai.event import TimelineEvent, TimelineSpan
from inspect_ai.log import read_eval_log

from orbit_audit.auditor import SeatPolicy
from orbit_audit.policy import ScriptedPolicy
from orbit_audit.runner import BRANCHES_KEY
from orbit_audit.timeline import build_auditor_timeline
from tests.contract.test_auditor import PURE, SHIP_IT, AuditorScript, act, auditor, seat_model
from tests.support.runs import info, run_audit, run_branches, sample
```

Append to `tests/contract/test_timeline.py`:

```python
def test_both_timelines_are_registered_and_persist_in_the_log(rolled_back):
    saved = read_eval_log(rolled_back.location).samples[0]
    assert [t.name for t in saved.timelines] == ["auditor", "default"]
    uuids = {e.uuid for e in saved.events}
    items = [leaf for lane in saved.timelines[0].root.content for node in lane.content
             for leaf in ([node] if isinstance(node, TimelineEvent) else node.content)]
    assert items
    assert all(item.event.uuid in uuids for item in items)


def test_a_bounded_transcript_loses_no_wakes(monkeypatch, tmp_path):
    monkeypatch.setenv("INSPECT_TRANSCRIPT_BOUNDED", "1")
    log, _ = run_branches(tmp_path, lambda: auditor(AuditorScript(rollback_rule)),
                          tool_replay=PURE)
    saved = read_eval_log(log.location).samples[0]
    timeline = next(t for t in saved.timelines if t.name == "auditor")
    wakes = [node for lane in timeline.root.content for node in lane.content
             if isinstance(node, TimelineSpan)]
    turns = [e for e in saved.events if e.event == "info" and isinstance(e.data, dict)
             and e.data.get("event") == "orbit_audit_auditor_turn"]
    assert len(wakes) == len(turns) > 0


def test_a_builder_failure_does_not_fail_the_run(monkeypatch, tmp_path):
    def boom(events):
        raise RuntimeError("boom")

    monkeypatch.setattr("orbit_audit.timeline.build_auditor_timeline", boom)
    log, _ = run_branches(tmp_path, lambda: auditor(AuditorScript(rollback_rule)),
                          tool_replay=PURE)  # run_branches asserts the run succeeded
    assert not sample(log).timelines
    [error] = info(log, "orbit_audit_timeline_error")
    assert (error["error"], error["message"]) == ("RuntimeError", "boom")


def test_a_run_without_auditor_wakes_registers_nothing(tmp_path):
    log, _ = run_branches(tmp_path, lambda: ScriptedPolicy())
    assert not sample(log).timelines
    assert not info(log, "orbit_audit_timeline_error")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/contract/test_timeline.py -v`
Expected: the 9 earlier tests pass. The three registration tests fail because nothing calls the builder yet: `test_both_timelines_are_registered…` and `test_a_bounded_transcript…` fail on `saved.timelines` being empty, and `test_a_builder_failure…` fails at `[error] = info(...)` with `ValueError: not enough values to unpack`. `test_a_run_without_auditor_wakes…` already passes; it pins the "register nothing" rule.

- [ ] **Step 3: Add `register_timelines` and call it from the solver**

In `src/orbit_audit/timeline.py`, merge these into the imports (keeping them sorted):

```python
import logging
```
```python
from inspect_ai.event import (
    Event,
    EventTreeSpan,
    Timeline,
    TimelineEvent,
    TimelineSpan,
    event_tree,
    timeline_build,
)
from inspect_ai.log import transcript
```

Add after the imports:

```python
logger = logging.getLogger(__name__)
```

Append to the module:

```python
def register_timelines(events: Sequence[Event]) -> None:
    """Register the ``auditor`` and ``default`` timelines on the current sample; never raises.

    Registering any timeline replaces the viewer's default one, and the viewer shows
    its timeline picker only for two or more, so ``default`` is registered with
    ``auditor``. A run with no auditor wakes registers nothing. A failure here is a
    view problem, not a run problem: it is logged and recorded as an
    ``orbit_audit_timeline_error`` event, and the viewer keeps its default view.
    """
    try:
        auditor = build_auditor_timeline(events)
        if auditor is None:
            return
        default = timeline_build(list(events), name="default")
        transcript().add_timeline(auditor)
        transcript().add_timeline(default)
    except Exception as error:
        logger.warning("orbit_audit: could not build the auditor timeline: %s", error)
        transcript().info(
            {"event": "orbit_audit_timeline_error", "error": type(error).__name__,
             "message": str(error)},
            source="orbit_audit",
        )
```

In `src/orbit_audit/runner.py`, add `from inspect_ai.log import transcript` to the `inspect_ai` imports (above `from inspect_ai.solver import …`), add `from orbit_audit.timeline import register_timelines` to the `orbit_audit` imports (after `from orbit_audit.scope import AuditScope`), and change the end of `solve`:

```python
        _adopt(state, completed or outcome, tree, budget)
        register_timelines(list(transcript().events))
        return state
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/contract/test_timeline.py -v`
Expected: 13 passed.

- [ ] **Step 5: Run the whole suite and lint**

Run: `.venv/bin/python -m pytest -q` then `.venv/bin/ruff check src tests`
Expected: all tests pass (nothing outside the timeline changes behavior; if an existing test fails, stop and investigate before continuing); ruff reports `All checks passed!`.

- [ ] **Step 6: Document where to find it**

Append to `docs/results.md`:

```markdown

## Reading the auditor's run in the viewer

Each audited sample carries an `auditor` timeline. In `inspect view`, open the
sample, go to its **Transcript** tab, and pick `auditor` in the timeline picker
at the top left (the other entry, `default`, is Inspect's own view, which does
not show branches).

- One lane per branch. A rolled-back branch's child is named
  `branch-1 (forked from branch-0 @ r17)`; its replayed prefix is not repeated.
- One sub-agent per auditor wake, named `r<revision> · <agent> · <boundary> —
  <ops>`, for example `r17 · pm · generation — replace_generation applied`.
  Open it for the auditor's model call (what it saw and its tool calls), its
  `orbit_audit_auditor_turn` event, and the interventions with their outcomes.
- Ops applied where the auditor was not woken (such as the `Halt` after
  `end_audit`) are listed directly in the branch's lane.
```

- [ ] **Step 7: Commit**

```bash
git add src/orbit_audit/timeline.py src/orbit_audit/runner.py tests/contract/test_timeline.py docs/results.md
git commit -m "$(cat <<'EOF'
feat: register the auditor and default timelines on every audited sample

A build failure is logged and recorded as orbit_audit_timeline_error; it never
fails the sample.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- Placement and registration of `auditor` + `default` after the branch loop: Task 3.
- Wake content, turn-event link, intervention join, wake name, `agent` span type: Tasks 1 and 2.
- Lanes in branch order, child lane name, branch events routed by `id`: Task 1.
- Edge rules (replays omitted, wake-less ops in the lane, no spans registers nothing): Tasks 1, 2 and 3.
- Non-fatal failure with warning and `orbit_audit_timeline_error`: Task 3.
- Tests 1-8 from the spec: 1 → Task 1 tests 1, 2, 4, 5; 2 → Task 1 test 3 and Task 2 rejected-op test; 3 → Task 2; 4 → Task 2; 5 → Task 3; 6 → Task 3; 7 → Task 3; 8 → Task 3.
- Docs note: Task 3, step 6 (`docs/results.md`, as the spec says).

**Placeholders:** none.

**Type consistency:** `build_auditor_timeline`, `register_timelines`, `_Wake`, `_wake_name`, `_lane_name`, `TURN_EVENTS`, `AUDITOR_SPAN` keep the same names and signatures across tasks; test helpers (`wakes_of`, `loose_of`, `info_in`, `lanes_of`, `head`, `rollback_rule`) are defined in Task 1 and reused unchanged.

**Review Focus:** items 1-2 are tests in Task 2; items 3-5 are tests in Task 3.
