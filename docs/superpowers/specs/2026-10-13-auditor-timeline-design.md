# Auditor timeline (issue #10, option C, first stage)

Date: 2026-10-13 · Branch: `feat/auditor-timeline` · Inspect: 0.3.263

## Problem

After an audited run, the auditor's activity is spread across the Inspect
viewer: auditor spans sit under whichever agent's execution the controller
hook ran in, its tool calls appear only inside model outputs, and whether an
operation was applied or rejected is only in a separate
`orbit_audit_intervention` event. The default timeline also dissolves the
`type="branch"` spans, so branches are invisible in it. Issue #10 asks for a
single place to read what the auditor saw, said, queued, and what happened to
each op, across branches, for Level 1 and Level 0.

## Goal and acceptance

Register an `auditor` timeline on each audited sample, so that in the viewer
(sample → Transcript → timeline dropdown → `auditor`) a reader can follow the
auditor's activity in order, across branches:

- what it saw and decided: the wake's model call (prompt, reasoning, tool calls);
- what it said and queued: the model call and the `orbit_audit_auditor_turn` event;
- whether each queued op was applied or rejected: in the wake's name, and in the
  nested `orbit_audit_intervention` events;
- rolled-back branches stay visible; it works for Level 1 and Level 0.

## What the spike established (throwaway probe, not kept)

Verified on a scripted L1 sample with one rollback (2 branches, 24 wakes), in
a default and a bounded (`INSPECT_TRANSCRIPT_BOUNDED=1`) transcript, and by
opening the saved log in the viewer:

- A timeline can be assembled from `transcript().events` by span ancestry. The
  auditor needs no root span of its own; all 24 `orbit_audit.auditor` spans
  were found whether nested directly under `experiment_loop`, under an agent,
  or under a tool span inside an agent.
- `transcript().events` returns the full history even when bounded.
  `transcript().history.resident_events` does not (101 of 349 events).
- A `Timeline` persists in `sample.timelines` and round-trips through
  `read_eval_log`. A `TimelineEvent` is stored as its event's UUID; every event
  involved had one.
- The viewer uses registered timelines **instead of** its default one, and shows
  the timeline dropdown only when there are two or more. So `auditor` must be
  registered together with a `default` timeline (`timeline_build(events,
  name="default")`).
- The default timeline unrolls `type="branch"` spans (documented behavior of
  `timeline_build` for unclassified span types), which explains the issue's
  "no branch span in the sidebar". A custom timeline shows them.
- A wake rendered as a sub-agent shows its model call, with the prompt the
  auditor was given, its tool calls (e.g. `replace_generation`) and the prior
  tool result.

## Design

### Placement

New module `src/orbit_audit/timeline.py`: a stateless function from the
sample's event list to a `Timeline`. `audit_solver.solve` calls it once, after
the branch loop and `_adopt`, and registers two timelines with
`transcript().add_timeline`: `auditor` and `default`. Nothing in
`controller.py`, `auditor/` or the event schemas changes.

### Building the timeline

Build `event_tree(transcript().events)`; every `orbit_audit.auditor` span is a
wake (the L1 span `auditor` and the L0 span `auditor-seat`).

- **Wake content, in order:** the span's events (model calls), then its turn
  event, then its interventions.
- **Turn event:** the `orbit_audit_auditor_turn` or `orbit_audit_seat_turn`
  info event (`source="orbit_audit"`) that follows the span among its siblings
  in the tree. If there is none (the wake raised), the wake is still shown,
  without one.
- **Interventions:** `orbit_audit_intervention` events with `replayed` false
  whose `(branch, revision)` equals the turn event's. A boundary has at most
  one wake, so the pair identifies it.
- **Wake name:** `r<revision> · <agent> · <boundary> — <ops>`, where `<ops>` is
  the comma-separated `<op> <outcome>` of its interventions, or `no ops`. The
  agent is omitted for boundaries without one; seat wakes carry a `(seat)`
  suffix. Inspect lowercases span names.
- **Span type:** `agent`, so the viewer renders each wake as a sub-agent.

Structure: root `auditor` → one lane per branch, in branch order → wakes. A
child lane is named `branch-1 (forked from branch-0 @ r17)`, taking `parent`
and `fork_revision` from its `orbit_audit_branch` `started` event; the
baseline is `branch-0`. The `orbit_audit_branch` events go into their lane by
the `id` in their data. Lane content is ordered by timestamp.

### Edge rules

- **Replayed interventions** (`replayed=true`, a child re-applying the parent's
  prefix) are omitted. They duplicate ops already shown in the parent's lane,
  and the child's lane name says its prefix is inherited.
- **Interventions with no wake** (for example the `Halt` the auditor queues
  after `end_audit` at a boundary where it is not woken) are shown directly in
  their lane. No intervention is dropped except replays.
- **No auditor spans in the run** (for example a `ScriptedPolicy`): register
  nothing. The viewer keeps its default view.

### Failure policy

A build error is non-fatal. It logs a warning and emits an
`orbit_audit_timeline_error` info event (exception type and message), and
registers no timeline, so the viewer's default view is unchanged. This
departs from the runner's "infrastructure failures fail loudly" rule on
purpose: the timeline is a view of a finished run and a bug in it should not
discard the sample. The tests below build the timeline for each scenario so a
regression fails in CI.

## Tests

`tests/contract/test_timeline.py`, using `run_branches` and the existing
scripted scenarios, reading `sample(log).timelines`:

1. L1 with rollback: timelines are `['auditor', 'default']`; lanes are
   `branch-0`, `branch-1` in order, the child's name carries its fork point;
   wake count equals the `orbit_audit_auditor_turn` count; each wake holds its
   model call, turn event and non-replayed interventions; no replayed
   intervention appears.
2. Outcomes in names: one applied and one rejected op (a controller-rejected
   op, as in `test_control_plane.py`; invalid tool arguments in
   `test_auditor.py` never become ledger records).
3. No-wake boundary: a scripted auditor calls `end_audit` at a non-`between_turns`
   wake (e.g. a generation). The `Halt` is then returned at a later
   `between_turns` boundary with no wake (`llm.py`, the `summary is not None`
   early return) and must appear directly in its lane. (If `end_audit` is
   called at a `between_turns` wake, the `Halt` is queued in that wake and
   nests there.)
4. L0 seat: same shape from `orbit_audit_seat_turn` and the `auditor-seat` span.
5. Persistence: every referenced event has a UUID and resolves after a
   `read_eval_log` round-trip.
6. Bounded transcript: with `INSPECT_TRANSCRIPT_BOUNDED=1` the wake count is
   unchanged.
7. Non-fatal failure: with the builder patched to raise, the run succeeds, no
   timeline is registered, and the error event is logged.
8. No auditor spans: a `ScriptedPolicy` run registers no timelines.

## Docs

A short note in `docs/results.md`: where to find the view (sample → Transcript
→ timeline dropdown → `auditor`) and how to read a wake's name.

## Out of scope

- The fork-aware branch view (`TimelineSpan.branches` / `branched_from`, anchor
  events), planned as a second spec.
- Auditor tool events (option D) and any change to events, ids or the
  controller (option B, dropped).

## Risks

- The timeline API and the viewer's "registered timelines replace the default"
  behavior were checked only against Inspect 0.3.263 (the pinned version).
  Recheck on upgrade.
- The wake-to-turn-event link relies on the turn event being emitted
  immediately after its span, in `LLMAuditor.decide` and `SeatPolicy.decide`.
  A test (1) fails if that ordering changes.
- The registered `default` is a snapshot taken when the solver ends, so it has
  no scoring section: the scorer runs after the solver, and no sample hook runs
  between scoring and logging. Scores and the scorer's events are still in the
  log and the viewer's other tabs. (Found in the final review; a later change
  could rebuild `default` once scoring is done.)
