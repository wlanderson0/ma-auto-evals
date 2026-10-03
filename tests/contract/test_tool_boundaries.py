"""Tool boundaries (§3.2 points 3 and 4): block, simulate and override."""

from __future__ import annotations

from orbit_audit.boundaries import Boundary
from orbit_audit.ops import BlockToolCall, OverrideToolResult, SimulateToolResult
from orbit_audit.policy import BoundaryView, ScriptedPolicy, at_tool
from tests.support.runs import info, model_events, run_audit, run_plain, sample, verdict
from tests.support.scenarios import AGENTS
from tests.support.scripted import agent_of

GREEN = "0 failed, 15 passed"


def policy(*rules):
    return lambda: ScriptedPolicy(*rules)


def test_runs(log) -> int:
    return sample(log).store.get("RepoEnv:test_runs", 0)


test_runs.__test__ = False  # a helper, not a test


def coder_tool_messages(log) -> list:
    last_coder = [e for e in model_events(log) if agent_of(e.input, AGENTS) == "coder"][-1]
    return [m for m in last_coder.input if m.role == "tool"]


def test_simulated_tool_result_has_no_side_effect_and_drives_behaviour(tmp_path):
    op = SimulateToolResult(agent="coder", result=GREEN)
    log, controller = run_audit(tmp_path, policy(at_tool(Boundary.TOOL_CALL, "coder", "run_tests", op)))
    assert test_runs(log) == 0
    assert verdict(log) == "APPROVE"
    [record] = controller.ledger.applied()
    assert (record.op, record.boundary, record.author) == ("simulate_tool_result", Boundary.TOOL_CALL, "auditor")
    [message] = coder_tool_messages(log)[:1]
    assert message.text == GREEN and record.orbit["tool_call_id"] == message.tool_call_id
    [control] = info(log, "orbit_tool_control")
    assert (control["tool"], control["executed"]) == ("run_tests", False)


def test_overridden_result_keeps_the_real_execution_as_evidence(tmp_path):
    op = OverrideToolResult(agent="coder", result=GREEN)
    log, controller = run_audit(tmp_path, policy(at_tool(Boundary.TOOL_RESULT, "coder", "run_tests", op)))
    assert test_runs(log) == 1
    assert verdict(log) == "APPROVE"
    [record] = controller.ledger.applied()
    assert record.original == "3 failed, 12 passed" and record.replacement == GREEN


def test_blocked_call_is_tool_feedback_without_effect(tmp_path):
    op = BlockToolCall(agent="coder", message="CI is down; tests unavailable.")
    log, controller = run_audit(tmp_path, policy(at_tool(Boundary.TOOL_CALL, "coder", "run_tests", op)))
    assert test_runs(log) == 0
    [message] = coder_tool_messages(log)[:1]
    assert "CI is down" in message.error.message
    assert controller.ledger.applied()[0].op == "block_tool_call"


def test_tool_ops_at_the_wrong_tool_boundary_are_rejected(tmp_path):
    plain = run_plain(tmp_path / "plain")
    op = OverrideToolResult(agent="coder", result=GREEN)  # needs TOOL_RESULT
    log, controller = run_audit(tmp_path / "a", policy(at_tool(Boundary.TOOL_CALL, "coder", "run_tests", op)))
    [record] = controller.ledger.rejected()
    assert "not allowed at boundary tool_call" in record.reason
    assert verdict(log) == verdict(plain) == "REJECT" and test_runs(log) == 1


def test_operations_pinned_to_another_tool_call_are_rejected(tmp_path):
    op = SimulateToolResult(agent="coder", result=GREEN, tool_call_id="not-this-call")
    log, controller = run_audit(tmp_path, policy(at_tool(Boundary.TOOL_CALL, "coder", "run_tests", op)))
    [record] = controller.ledger.rejected()
    assert "is for tool call 'not-this-call'" in record.reason
    assert test_runs(log) == 1


def test_tool_views_carry_the_call_and_result(tmp_path):
    views: list[BoundaryView] = []

    class Watch(ScriptedPolicy):
        async def decide(self, view):
            views.append(view)
            return []

    run_audit(tmp_path, Watch)
    [call] = [v for v in views if v.boundary == Boundary.TOOL_CALL and v.tool_name == "run_tests"]
    [result] = [v for v in views if v.boundary == Boundary.TOOL_RESULT and v.tool_name == "run_tests"]
    assert call.tool_call_id == result.tool_call_id is not None
    assert result.tool_result == "3 failed, 12 passed" and result.revision > call.revision
