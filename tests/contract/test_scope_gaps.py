"""Known gaps in the scope check (lean/scope-map.md, G1 and G2), as expected failures.

Each test asserts the behaviour the scope promises. They fail today; ``strict=True`` makes
the fix visible (an unexpected pass) so the Lean model and its ``bug_`` theorems are
updated with it.
"""

from __future__ import annotations

import pytest

from orbit_audit.boundaries import Boundary
from orbit_audit.ops import AmendPending, ReplaceGeneration
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


@pytest.mark.xfail(strict=True, reason="G2: an empty agent gives an empty affected")
def test_g2_empty_agent_cannot_write_an_out_of_scope_generation(tmp_path):
    write = ReplaceGeneration(agent="", content="all tests pass")

    def rule(view: BoundaryView):
        if (view.boundary, view.agent, view.generation) == (Boundary.GENERATION, "coder", 1):
            return write
        return None

    scope = AuditScope(ops={"replace_generation"}, agents={"pm"})
    _, controller = run_audit(tmp_path, lambda: ScriptedPolicy(rule), scope=scope)
    assert controller.ledger.applied() == ()
