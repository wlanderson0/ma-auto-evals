"""The differential tests catch broken Python: each mutant below must produce a mismatch.

A mutant is patched in for one comparison only. If a mutant survives, the harness is
not checking what the Lean model is about (plan step 5, adequacy).
"""

from __future__ import annotations

import pytest

from orbit_audit import controller as controller_module
from orbit_audit import ops as ops_module
from orbit_audit import scope as scope_module
from orbit_audit.controller import AuditController
from orbit_audit.delivery import DeliveryOverlay
from tests.lean.test_differential import (
    DECIDE_EXAMPLES,
    SEAT,
    _world,
    compare_check,
    compare_decide,
    compare_effects,
    compare_problem,
)

DROP_ALL = {"op": "drop_message", "message_id": "m0", "recipients": None}
DROP_BOB = {"op": "drop_message", "message_id": "m0", "recipients": ["bob"]}
AT_BOB = {"boundary": "generation", "revision": 5, "agent": "bob", "tool_call_id": None}


def _ignore_agents(self, op, world=None):
    return None if self.allows_op(op.name) else f"{op.name} is outside this audit's scope"


def _explicit_only(self, world):
    return self.agents()


def _no_targeted_check(original):
    def problem(self, view, op, family):
        return original(self, view.__class__(**{**view.__dict__, "agent": None}), op, family)
    return problem


def _deliver_ignoring_recipient_drops(original):
    def deliver(self, request):
        saved = {d for d in self.drops if d[1] is not None}
        self.drops -= saved
        try:
            return original(self, request)
        finally:
            self.drops |= saved
    return deliver


def _decide_example(i):
    scope, boundaries, agent, ops = DECIDE_EXAMPLES[i]
    return lambda lean: compare_decide(lean, _world(), scope, boundaries, agent, None, ops)


def _rollback_without_scope(original):
    def problem(self, view, op):
        saved, self.scope = self.scope, None
        try:
            return original(self, view, op)
        finally:
            self.scope = saved
    return problem


def _agent_required(original):
    """The G2 fix: an op naming no agent is refused. The model mirrors today's code, so the
    differential tests must notice the fix (and the model must then be updated)."""
    def problem(self, op, world=None):
        if getattr(op, "agent", None) == "":
            return f"{op.name} is outside this audit's scope (no agent)"
        return original(self, op, world)
    return problem


MUTANTS = [
    ("scope ignores agents", lambda p: p.setattr(scope_module.AuditScope, "problem",
                                                  _ignore_agents),
     lambda lean: compare_problem(lean, _world(), SEAT, DROP_ALL)),
    ("affected ignores implicit recipients",
     lambda p: p.setattr(ops_module._ExistingMessageOp, "affected", _explicit_only),
     lambda lean: compare_problem(lean, _world(), SEAT, DROP_ALL)),
    ("controller skips the targeted check",
     lambda p: p.setattr(AuditController, "_problem",
                         _no_targeted_check(AuditController._problem)),
     lambda lean: compare_check(lean, _world(), None, AT_BOB,
                                {"op": "replace_generation", "agent": "alice", "content": "x",
                                 "tool_calls": []})),
    ("delivery ignores per-recipient drops",
     lambda p: p.setattr(DeliveryOverlay, "deliver",
                         _deliver_ignoring_recipient_drops(DeliveryOverlay.deliver)),
     lambda lean: compare_effects(lean, _world(), DROP_BOB)),
    ("no sole-producer rule", lambda p: p.setattr(controller_module, "_SOLE", {}),
     _decide_example(1)),
    ("rollback skips the scope check",
     lambda p: p.setattr(AuditController, "_rollback_problem",
                         _rollback_without_scope(AuditController._rollback_problem)),
     _decide_example(6)),
    ("recipients [] read as None",
     lambda p: p.setattr(ops_module._ExistingMessageOp, "agents",
                         lambda self: tuple(sorted(self.recipients or ())) or ("bob", "carol")),
     lambda lean: compare_problem(lean, _world(), SEAT,
                                  {"op": "drop_message", "message_id": "missing",
                                   "recipients": []})),
    ("G2 fixed in the Python only",
     lambda p: p.setattr(scope_module.AuditScope, "problem",
                         _agent_required(scope_module.AuditScope.problem)),
     lambda lean: compare_problem(lean, _world(), SEAT,
                                  {"op": "replace_generation", "agent": "", "content": "x",
                                   "tool_calls": []})),
]


@pytest.mark.parametrize("name,patch,compare", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_mutant_is_caught(lean, name, patch, compare):
    assert compare(lean) == [], f"unmutated code already differs for {name}"
    with pytest.MonkeyPatch.context() as p:
        patch(p)
        assert compare(lean) != [], f"mutant survived: {name}"
