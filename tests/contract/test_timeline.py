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
from inspect_ai.log import read_eval_log

from orbit_audit.auditor import SeatPolicy
from orbit_audit.policy import ScriptedPolicy
from orbit_audit.runner import BRANCHES_KEY
from orbit_audit.timeline import build_auditor_timeline
from tests.contract.test_auditor import PURE, SHIP_IT, AuditorScript, act, auditor, seat_model
from tests.support.runs import info, run_audit, run_branches, sample


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
