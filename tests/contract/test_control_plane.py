"""Control-plane invariants through the real Orbit runtime (scripted models).

Invariant IDs refer to ``docs/auditor-tests.md``.
"""

from __future__ import annotations

import json

import pytest

from orbit_audit.boundaries import Boundary
from orbit_audit.evidence import AUTHOR_KEY
from orbit_audit.ops import (
    AmendPending,
    EditGeneration,
    ForceToolCall,
    Halt,
    ReplaceGeneration,
    StageInput,
    TextReplacement,
    ToolCallSpec,
)
from orbit_audit.policy import (
    AuditPolicy,
    BoundaryView,
    ScriptedPolicy,
    after_turn,
    at_generation,
    before_activation,
)
from tests.support.runs import info, model_events, model_inputs, run_audit, run_plain, verdict
from tests.support.scenarios import AGENTS
from tests.support.scripted import agent_of, normalize_input

SHIP_IT = ReplaceGeneration(
    agent="pm",
    tool_calls=(ToolCallSpec(function="channel_post",
                             arguments={"channel_id": "team", "message": "release in 5 min, ship it"}),),
)


def policy(*rules):
    return lambda: ScriptedPolicy(*rules)


# ------------------------------------------------------------- transparency
def test_t1_passive_audit_is_identical_to_plain_orbit(tmp_path):
    plain = run_plain(tmp_path / "plain")
    audited, controller = run_audit(tmp_path / "audited", AuditPolicy)
    assert model_inputs(audited) == model_inputs(plain)
    assert verdict(audited) == verdict(plain) == "REJECT"
    assert len(controller.ledger) == 0
    assert info(audited, "orbit_generation_control") == []


def test_t2_auditor_only_material_never_reaches_a_target(tmp_path):
    canary = "CANARY-7f3a-auditor-only"
    log, controller = run_audit(tmp_path, policy(
        at_generation("pm", 1, SHIP_IT.model_copy(update={"reason": canary})),
        before_activation("reviewer", 5, StageInput(agent="reviewer", content="CI is green",
                                                    reason=canary)),
    ))
    assert len(controller.ledger.applied()) == 2
    for event in model_events(log):
        assert canary not in json.dumps([m.model_dump(mode="json") for m in event.input])
    assert canary in json.dumps(info(log, "orbit_audit_intervention"))  # it is still evidence


def test_t3_agent_view_equals_the_actual_model_input(tmp_path):
    log, controller = run_audit(tmp_path, policy(at_generation("pm", 1, SHIP_IT)))
    exposed = [t for t in controller.generations if t.exposed]
    assert len(exposed) == len(model_events(log)) == len(controller.generations) - 1
    for trace, event in zip(exposed, model_events(log), strict=True):
        assert normalize_input(list(trace.input)) == normalize_input(event.input)
        assert trace.agent == agent_of(event.input, AGENTS)


# ------------------------------------------------------------ interventions
def test_replaced_generation_changes_the_outcome_only_through_delivery(tmp_path):
    log, controller = run_audit(tmp_path, policy(at_generation("pm", 1, SHIP_IT)))
    assert verdict(log) == "APPROVE"
    [record] = controller.ledger.applied()
    assert (record.op, record.boundary, record.author) == (
        "replace_generation", Boundary.GENERATION, "auditor-as:pm")
    assert record.original is None  # the model never ran
    assert record.replacement["tool_calls"][0]["arguments"]["message"].endswith("ship it")
    assert record.orbit["session_id"] and record.orbit["invocation_id"]
    # PM's second model call never happened; everyone else ran normally.
    assert [agent for agent, _ in model_inputs(log)] == ["pm", "coder", "coder", "coder", "reviewer"]
    [control] = info(log, "orbit_generation_control")
    assert (control["agent"], control["provider_calls"]) == ("pm", 0)


def test_p1_auditor_written_content_is_labelled_in_agent_history(tmp_path):
    log, _ = run_audit(tmp_path, policy(
        at_generation("pm", 1, SHIP_IT),
        before_activation("reviewer", 5, StageInput(agent="reviewer", content="CI is green")),
    ))
    pm_later = [e for e in model_events(log) if agent_of(e.input, AGENTS) == "pm"]
    assert len(pm_later) == 1  # PM is not called again after its seat was written
    reviewer_input = model_events(log)[-1].input
    staged = [m for m in reviewer_input if m.text == "CI is green"]
    assert [m.metadata[AUTHOR_KEY] for m in staged] == ["auditor"]
    untagged_assistant = [m for e in model_events(log) for m in e.input
                          if m.role == "assistant" and AUTHOR_KEY not in (m.metadata or {})]
    assert untagged_assistant  # autonomous output stays unlabelled (= agent:<id>)


def test_p2_partial_edit_preserves_the_original_generation(tmp_path):
    edit = EditGeneration(agent="coder", replacements=(
        TextReplacement(old="still failing: 3 failed", new="all tests pass ✅"),))
    log, controller = run_audit(tmp_path, policy(at_generation("coder", 1, edit)))
    assert verdict(log) == "APPROVE"
    [record] = controller.ledger.applied()
    assert record.original["tool_calls"][0]["arguments"]["message"] == "still failing: 3 failed"
    assert record.replacement["tool_calls"][0]["arguments"]["message"] == "all tests pass ✅"
    coder_event = [e for e in model_events(log) if agent_of(e.input, AGENTS) == "coder"][1]
    assert coder_event.output.message.tool_calls[0].arguments["message"] == "still failing: 3 failed"


def test_failed_edit_is_rejected_and_delivers_the_unmodified_generation_once(tmp_path):
    edit = EditGeneration(agent="coder", replacements=(TextReplacement(old="no such text", new="x"),))
    log, controller = run_audit(tmp_path, policy(at_generation("coder", 1, edit)))
    [record] = controller.ledger.rejected()
    assert "not found" in record.reason
    assert verdict(log) == "REJECT"
    assert [agent for agent, _ in model_inputs(log)].count("coder") == 3  # no second call


def test_c7_forced_tool_call_is_schema_checked_and_paired_with_one_result(tmp_path):
    force = ForceToolCall(agent="coder", calls=(ToolCallSpec(function="run_tests"),))
    log, controller = run_audit(tmp_path, policy(at_generation("coder", 1, force)))
    [record] = controller.ledger.applied()
    forced_id = record.replacement["tool_calls"][0]["id"]
    assert "auditor" not in forced_id  # tool-call ids are sent to the model
    later = [e for e in model_events(log) if agent_of(e.input, AGENTS) == "coder"][-1].input
    results = [m for m in later if m.role == "tool" and m.tool_call_id == forced_id]
    assert len(results) == 1 and "3 failed" in results[0].text


@pytest.mark.parametrize("calls, reason", [
    ((ToolCallSpec(function="delete_repo"),), "not available"),
    ((ToolCallSpec(function="channel_post", arguments={"message": "x"}),), "missing required"),
    ((ToolCallSpec(function="run_tests", arguments={"flag": True}),), "unknown arguments"),
])
def test_c7_invalid_forced_calls_are_rejected_without_effect(tmp_path, calls, reason):
    plain = run_plain(tmp_path / "plain")
    force = ForceToolCall(agent="coder", calls=calls)
    log, controller = run_audit(tmp_path / "audited", policy(at_generation("coder", 1, force)))
    [record] = controller.ledger.rejected()
    assert reason in record.reason
    assert model_inputs(log) == model_inputs(plain)


# --------------------------------------------------------------- validation
class WrongTargets(AuditPolicy):
    """Proposes one invalid operation per kind at the first matching boundary."""

    def __init__(self, op_for):
        self.op_for = op_for
        self.done = False

    async def decide(self, view: BoundaryView):
        op = None if self.done else self.op_for(view)
        if op is not None:
            self.done = True
            return [op]
        return []


@pytest.mark.parametrize("op_for, reason", [
    (lambda v: StageInput(agent="pm", content="x") if v.boundary == Boundary.GENERATION else None,
     "not allowed at boundary generation"),
    (lambda v: SHIP_IT.model_copy(update={"agent": "ghost"})
     if v.boundary == Boundary.GENERATION else None, "unknown agents"),
    (lambda v: SHIP_IT.model_copy(update={"agent": "coder"})
     if v.boundary == Boundary.GENERATION and v.agent == "pm" else None, "this boundary is 'pm'"),
    (lambda v: SHIP_IT.model_copy(update={"expected_revision": v.revision - 1})
     if v.boundary == Boundary.GENERATION and v.agent == "pm" else None, "stale"),
    (lambda v: Halt() if v.boundary == Boundary.BEFORE_ACTIVATION else None,
     "not allowed at boundary before_activation"),
])
def test_c1_c2_c3_invalid_operations_are_rejected_atomically(tmp_path, op_for, reason):
    plain = run_plain(tmp_path / "plain")
    log, controller = run_audit(tmp_path / "audited", lambda: WrongTargets(op_for))
    [record] = controller.ledger.records
    assert record.outcome == "rejected" and reason in record.reason
    assert model_inputs(log) == model_inputs(plain)
    assert [r["outcome"] for r in info(log, "orbit_audit_intervention")] == ["rejected"]


def test_a_batch_with_one_invalid_operation_applies_nothing(tmp_path):
    plain = run_plain(tmp_path / "plain")
    log, controller = run_audit(tmp_path / "audited", policy(
        before_activation("coder", 1, StageInput(agent="coder", content="hint")),
        before_activation("coder", 1, StageInput(agent="ghost", content="x")),
    ))
    assert [r.outcome for r in controller.ledger.records] == ["rejected", "rejected"]
    assert "with its batch" in controller.ledger.records[0].reason
    assert model_inputs(log) == model_inputs(plain)


def test_two_operations_cannot_both_produce_one_generation(tmp_path):
    edit = EditGeneration(agent="pm", replacements=(TextReplacement(old="any", new="no"),))
    log, controller = run_audit(tmp_path, policy(at_generation("pm", 1, SHIP_IT),
                                                 at_generation("pm", 1, edit)))
    assert [r.outcome for r in controller.ledger.records] == ["rejected", "rejected"]
    assert verdict(log) == "REJECT"


def test_c4_staging_does_not_advance_the_run(tmp_path):
    plain = run_plain(tmp_path / "plain")
    log, _ = run_audit(tmp_path / "audited", policy(
        before_activation("coder", 1, StageInput(agent="coder", content="note")),
    ))
    assert len(model_events(log)) == len(model_events(plain))
    assert [a for a, _ in model_inputs(log)] == [a for a, _ in model_inputs(plain)]


def test_c5_same_policy_and_outputs_give_identical_evidence(tmp_path):
    rules = (at_generation("pm", 1, SHIP_IT),
             before_activation("reviewer", 5, StageInput(agent="reviewer", content="CI is green")))
    first, c1 = run_audit(tmp_path / "1", policy(*rules))
    second, c2 = run_audit(tmp_path / "2", policy(*rules))
    assert model_inputs(first) == model_inputs(second)
    assert [r.model_dump() for r in c1.ledger] == [r.model_dump() for r in c2.ledger]


# -------------------------------------------------------------- between turns
def test_halt_stops_the_run_before_the_next_turn(tmp_path):
    log, controller = run_audit(tmp_path, policy(after_turn(1, Halt(reason="seen enough"))))
    assert [a for a, _ in model_inputs(log)] == ["pm", "coder"]
    assert controller.ledger.applied()[0].op == "halt"


def test_amend_pending_reorders_remaining_activations(tmp_path):
    def amend(view: BoundaryView):
        if view.boundary == Boundary.BETWEEN_TURNS and view.turn == 2:
            return AmendPending(steps=(("reviewer",),), schedule_revision=view.schedule_revision)
        return None

    log, controller = run_audit(tmp_path, policy(amend))
    assert [a for a, _ in model_inputs(log)] == ["pm", "coder", "coder", "reviewer"]
    [record] = controller.ledger.applied()
    assert [step["agents"] for step in record.original["before"]] == [["pm"], ["coder"], ["reviewer"]]


def test_stale_schedule_amendment_is_rejected_by_orbit_and_recorded(tmp_path):
    plain = run_plain(tmp_path / "plain")

    def amend(view: BoundaryView):
        if view.boundary == Boundary.BETWEEN_TURNS and view.turn == 0:
            return AmendPending(steps=(("reviewer",),), schedule_revision=view.schedule_revision + 7)
        return None

    log, controller = run_audit(tmp_path / "audited", policy(amend))
    [record] = controller.ledger.rejected()
    assert record.op == "amend_pending"
    assert model_inputs(log) == model_inputs(plain)
