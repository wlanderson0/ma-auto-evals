"""The pure rule that finds each agent's final model request (issue #19, stage 1)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from orbit_audit.controller import GenerationTrace
from orbit_audit.measurements import (
    Alignment,
    Gen,
    MeasurementRecorder,
    ModelCall,
    align_requests,
    failed_model_calls,
    stored_requests,
    target_model_calls,
)
from orbit_audit.timeline import AUDITOR_SPAN


def live(agent, index):
    return Gen(agent, index, exposed=True, replayed=False)


def replayed(agent, index):
    return Gen(agent, index, exposed=True, replayed=True)


def written(agent, index):
    return Gen(agent, index, exposed=False, replayed=False)


def test_live_generations_map_to_their_events_in_order():
    result = align_requests(
        branch="branch-0", parent=None,
        generations=[live("bob", 0), live("bob", 1), live("bob", 2)],
        calls=[ModelCall("e0", "bob", "branch-0"), ModelCall("e1", "bob", "branch-0"),
               ModelCall("e2", "bob", "branch-0")])
    assert result.lists["bob"] == [("e0", "branch-0"), ("e1", "branch-0"), ("e2", "branch-0")]
    assert stored_requests(result)["bob"] == {"event": "e2", "via": "branch-0", "why": ""}


def test_other_agents_and_other_branches_are_not_matched():
    result = align_requests(
        branch="branch-1", parent=None, generations=[live("bob", 0)],
        calls=[ModelCall("x", "carol", "branch-1"), ModelCall("y", "bob", "branch-0"),
               ModelCall("z", "bob", "branch-1")])
    assert result.lists["bob"] == [("z", "branch-1")]


def test_a_replayed_prefix_is_taken_from_the_parent_at_the_same_index():
    parent = {"bob": [("p0", "branch-0"), ("p1", "branch-0"), ("p2", "branch-0")]}
    result = align_requests(
        branch="branch-1", parent=parent,
        generations=[replayed("bob", 0), live("bob", 1), live("bob", 2)],
        calls=[ModelCall("c1", "bob", "branch-1"), ModelCall("c2", "bob", "branch-1")])
    assert result.lists["bob"] == [("p0", "branch-0"), ("c1", "branch-1"), ("c2", "branch-1")]


def test_an_agent_replayed_in_full_points_at_its_own_last_generation_not_the_parents_last():
    parent = {"bob": [("p0", "branch-0"), ("p1", "branch-0"), ("p2", "branch-0")]}
    result = align_requests(
        branch="branch-1", parent=parent,
        generations=[replayed("bob", 0), replayed("bob", 1)], calls=[])
    assert stored_requests(result)["bob"] == {"event": "p1", "via": "branch-0", "why": ""}


def test_a_grandparent_event_keeps_its_owning_branch():
    parent = {"bob": [("g0", "branch-0")]}
    result = align_requests(branch="branch-2", parent=parent,
                            generations=[replayed("bob", 0)], calls=[])
    assert stored_requests(result)["bob"]["via"] == "branch-0"


def test_auditor_written_generations_in_the_middle_do_not_shift_later_events():
    result = align_requests(
        branch="branch-0", parent=None,
        generations=[live("alice", 0), written("alice", 1), live("alice", 2)],
        calls=[ModelCall("a0", "alice", "branch-0"), ModelCall("a2", "alice", "branch-0")])
    assert result.lists["alice"] == [("a0", "branch-0"), None, ("a2", "branch-0")]


def test_a_last_generation_the_auditor_wrote_has_no_event_and_says_why():
    result = align_requests(
        branch="branch-0", parent=None, generations=[live("alice", 0), written("alice", 1)],
        calls=[ModelCall("a0", "alice", "branch-0")])
    assert stored_requests(result)["alice"] == {
        "event": None, "via": None,
        "why": "the agent's last generation has no model event of its own"}


def test_a_count_mismatch_is_reported_not_guessed():
    result = align_requests(
        branch="branch-0", parent=None, generations=[live("bob", 0), live("bob", 1)],
        calls=[ModelCall("only", "bob", "branch-0")])
    assert result.lists["bob"] == [None, None]
    stored = stored_requests(result)["bob"]
    assert stored["event"] is None and "2 live generation" in stored["why"]


def test_more_events_than_live_generations_is_reported_not_guessed():
    result = align_requests(
        branch="branch-0", parent=None, generations=[live("bob", 0)],
        calls=[ModelCall("e0", "bob", "branch-0"), ModelCall("e1", "bob", "branch-0")])
    assert result.lists["bob"] == [None]
    stored = stored_requests(result)["bob"]
    assert stored["event"] is None
    assert stored["why"] == "1 live generation(s) but 2 model event(s) in branch-0"


def test_a_parent_list_shorter_than_the_replayed_index_is_unknown():
    parent = {"bob": [("p0", "branch-0")]}
    result = align_requests(
        branch="branch-1", parent=parent,
        generations=[replayed("bob", 0), replayed("bob", 1)], calls=[])
    assert result.lists["bob"] == [("p0", "branch-0"), None]


def test_a_failed_model_call_is_named_as_the_reason_not_a_count_mismatch():
    result = align_requests(
        branch="branch-0", parent=None, generations=[live("bob", 0), live("bob", 1)],
        calls=[ModelCall("e0", "bob", "branch-0")],
        failed=[ModelCall("f1", "bob", "branch-0"), ModelCall("x", "bob", "branch-1")])
    assert result.lists["bob"] == [None, None]
    assert stored_requests(result)["bob"] == {
        "event": None, "via": None,
        "why": "its model call failed (1 failed call(s) in branch-0)"}


def test_a_mismatch_the_failed_calls_do_not_explain_is_still_a_count_mismatch():
    result = align_requests(
        branch="branch-0", parent=None, generations=[live("bob", 0), live("bob", 1)],
        calls=[], failed=[ModelCall("f", "bob", "branch-0")])
    assert stored_requests(result)["bob"]["why"] == (
        "2 live generation(s) but 0 model event(s) in branch-0")


def test_a_replayed_generation_with_no_parent_record_is_unknown():
    result = align_requests(branch="branch-1", parent=None,
                            generations=[replayed("bob", 0)], calls=[])
    assert result.lists["bob"] == [None]
    assert isinstance(result, Alignment)


def trace(agent, index, *, exposed=True, replayed=False):
    return GenerationTrace(agent=agent, index=index, turn=None, input=(), tools=(),
                           exposed=exposed, replayed=replayed)


RUNTIME = object()


def fake(branch, generations, authors=lambda: {}, runtime=RUNTIME):
    return SimpleNamespace(branch=branch, generations=generations, authors=authors,
                           runtime=runtime)


def test_the_recorder_keeps_each_branch_and_threads_the_parents_lists_to_children():
    recorder = MeasurementRecorder()
    parent_event = SimpleNamespace(
        event="model", pending=False, error=None, uuid="p0", span_id="s-model")
    spans = [
        SimpleNamespace(event="span_begin", id="s-branch", parent_id=None, type="branch",
                        name="branch-0"),
        SimpleNamespace(event="span_begin", id="s-agent", parent_id="s-branch", type="agent",
                        name="bob"),
        SimpleNamespace(event="span_begin", id="s-model", parent_id="s-agent",
                        type="orbit.model", name="model_generate"),
    ]
    recorder.record(fake("branch-0", [trace("bob", 0)], authors=lambda: {"m1": "agent:bob"}),
                    None, [*spans, parent_event])
    recorder.record(fake("branch-1", [trace("bob", 0, replayed=True)]), "branch-0", [])
    payload = recorder.payload()
    assert payload["version"] == 1
    assert payload["branches"]["branch-0"]["authors"] == {"m1": "agent:bob"}
    assert payload["branches"]["branch-0"]["generations"] == {"bob": 1}
    assert payload["branches"]["branch-0"]["requests"]["bob"]["event"] == "p0"
    assert payload["branches"]["branch-1"]["requests"]["bob"] == {
        "event": "p0", "via": "branch-0", "why": ""}


def test_a_branch_without_a_runtime_records_unknown_authors():
    recorder = MeasurementRecorder()
    recorder.record(fake("branch-0", [], runtime=None), None, [])
    assert recorder.payload()["branches"]["branch-0"]["authors"] is None


def test_a_failing_authors_call_is_recorded_and_logged_not_raised(caplog):
    def boom():
        raise KeyError("m9")

    recorder = MeasurementRecorder()
    with caplog.at_level(logging.WARNING, logger="orbit_audit.measurements"):
        recorder.record(fake("branch-0", [], authors=boom), None, [])
    record = recorder.payload()["branches"]["branch-0"]
    assert record["authors"] is None and "KeyError" in record["authors_error"]
    assert any("branch-0" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


def test_a_working_authors_call_leaves_no_error_and_no_warning(caplog):
    recorder = MeasurementRecorder()
    with caplog.at_level(logging.WARNING, logger="orbit_audit.measurements"):
        recorder.record(fake("branch-0", [], authors=lambda: {"m1": "agent:bob"}), None, [])
    assert "authors_error" not in recorder.payload()["branches"]["branch-0"]
    assert not caplog.records


def test_a_failing_record_is_noted_and_the_sample_goes_on():
    class Broken:
        branch = "branch-0"
        runtime = RUNTIME

        @property
        def generations(self):
            raise RuntimeError("bad trace")

    recorder = MeasurementRecorder()
    recorder.record(Broken(), None, [])
    record = recorder.payload()["branches"]["branch-0"]
    assert record["authors"] is None and record["requests"] == {}
    assert "RuntimeError: bad trace" == record["record_error"]
    recorder.record(fake("branch-1", [trace("bob", 0, replayed=True)]), "branch-0", [])
    assert recorder.payload()["branches"]["branch-1"]["requests"]["bob"]["event"] is None


def test_the_auditors_own_model_calls_are_not_target_calls():
    def begin(id, parent, type, name):
        return SimpleNamespace(event="span_begin", id=id, parent_id=parent, type=type,
                               name=name)

    def call(uuid, span):
        return SimpleNamespace(event="model", pending=False, error=None, uuid=uuid,
                               span_id=span)

    events = [
        begin("b", None, "branch", "branch-0"), begin("a", "b", "agent", "bob"),
        begin("aud", "a", AUDITOR_SPAN, "auditor"), begin("m1", "aud", "orbit.model", "gen"),
        begin("m2", "a", "orbit.model", "gen"),
        call("auditor-call", "m1"), call("target-call", "m2"),
    ]
    assert target_model_calls(events) == [ModelCall("target-call", "bob", "branch-0")]


def test_errored_target_calls_are_found_apart_from_the_auditors_and_the_ok_ones():
    def begin(id, parent, type, name):
        return SimpleNamespace(event="span_begin", id=id, parent_id=parent, type=type,
                               name=name)

    def call(uuid, span, error=None):
        return SimpleNamespace(event="model", pending=False, error=error, uuid=uuid,
                               span_id=span)

    events = [
        begin("b", None, "branch", "branch-0"), begin("a", "b", "agent", "bob"),
        begin("aud", "a", AUDITOR_SPAN, "auditor"),
        call("ok", "a"), call("broken", "a", error="boom"), call("auditor-broken", "aud", "boom"),
    ]
    assert target_model_calls(events) == [ModelCall("ok", "bob", "branch-0")]
    assert failed_model_calls(events) == [ModelCall("broken", "bob", "branch-0")]
