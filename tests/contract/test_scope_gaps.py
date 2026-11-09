"""Gaps in the scope check (lean/scope-map.md): G1 open, G2 fixed.

Each test asserts the behaviour the scope promises. An open gap is a strict expected
failure, so its fix shows up as an unexpected pass and the Lean model and its ``bug_``
theorem are updated with it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orbit_audit.boundaries import Boundary
from orbit_audit.ops import AmendPending, OpError, ReplaceGeneration, parse_op
from orbit_audit.policy import AuditPolicy, BoundaryView, ScriptedPolicy
from orbit_audit.scope import AuditScope
from tests.support.runs import run_audit, run_branches


class DropOthersTurns(AuditPolicy):
    """At the first pause, replace the pending steps with one turn for pm."""

    name = "drop-others-turns"

    def __init__(self) -> None:
        self.done = False

    async def decide(self, view):
        if view.boundary == Boundary.BETWEEN_TURNS and not self.done and view.pending_steps:
            self.done = True
            return [AmendPending(steps=(("pm",),), schedule_revision=view.schedule_revision)]
        return []


@pytest.mark.xfail(strict=True, reason="G1: amend_pending lists only the new steps' agents")
def test_g1_amend_pending_cannot_remove_out_of_scope_turns(tmp_path):
    scope = AuditScope(ops={"amend_pending"}, agents={"pm"})
    _, [controller] = run_branches(tmp_path, DropOthersTurns, scope=scope)
    assert controller.ledger.applied() == ()
    assert {"coder", "reviewer"} <= {trace.agent for trace in controller.generations}


@pytest.mark.parametrize("name,fields", [
    ("replace_generation", {"content": "x"}),
    ("edit_generation", {"replacements": [{"old": "a", "new": "b"}]}),
    ("force_tool_call", {"calls": [{"function": "f"}]}),
    ("stage_input", {"content": "x"}),
    ("block_tool_call", {}),
    ("simulate_tool_result", {"result": "x"}),
    ("override_tool_result", {"result": "x"}),
])
def test_g2_an_op_cannot_name_no_agent(name, fields):
    with pytest.raises((ValidationError, OpError)):
        parse_op({"op": name, "agent": "", **fields})


def test_g2_an_op_lands_only_on_the_agent_it_names(tmp_path):
    """The name the scope check judged is the agent the op changes."""
    write = ReplaceGeneration(agent="pm", content="all tests pass")

    def rule(view: BoundaryView):
        if (view.boundary, view.agent, view.generation) == (Boundary.GENERATION, "coder", 1):
            return write
        return None

    scope = AuditScope(ops={"replace_generation"}, agents={"pm"})
    _, controller = run_audit(tmp_path, lambda: ScriptedPolicy(rule), scope=scope)
    assert controller.ledger.applied() == ()
    [record] = controller.ledger.rejected()
    assert "but this boundary is 'coder'" in record.reason
