"""The rollback model against real branches (rollback plan step 5, ``lean/rollback-map.md``).

Scripted release-team audits with Hypothesis-drawn rollbacks: where each lands, which
interventions it carries (well-targeted, aimed at another agent as in RB1, two producers),
an applied or vetoed op below the fork, and a second rollback in the child, within or past
its own fork (RB3). For every child branch:

* **Model agreement.** The Lean driver gets the parent's recorded steps and what the child
  saw, and computes with `Branch.lean`'s own functions the batch at each boundary
  (`batchAt`), whether the fingerprint check ends it (`desynced`) and how a run that ends on
  its own is classified (`finished`, RB2). They must match what the Python did.
* **Fidelity on real runs** (property 1, empirically for Orbit and the scripted models):
  unless the child's fork is past its parent's own fork, it never desyncs, and below the
  fork it has its parent's fingerprints and applies its parent's ops; a completed child
  reached its fork.

Fault injection exercises what scripted runs never do on their own: a tool that is not
really pure (a desync) and a run that halts before the fork (RB2).
"""

from __future__ import annotations

import math
import os
from typing import Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from inspect_ai import eval as inspect_eval
from inspect_ai.model import get_model
from inspect_ai.tool import Tool, tool
from orbit import ToolFactory
from orbit.ids import id_scope, sequential_ids

from orbit_audit.boundaries import Boundary
from orbit_audit.branches import BranchStatus
from orbit_audit.controller import AuditController
from orbit_audit.ops import (
    BlockToolCall,
    OverrideToolResult,
    ReplaceGeneration,
    Rollback,
    StageInput,
)
from orbit_audit.policy import AuditPolicy, BoundaryView
from orbit_audit.runner import BRANCHES_KEY
from orbit_audit.task import audit_task
from tests.lean.driver import lean_op
from tests.support.runs import run_branches, sample
from tests.support.scenarios import release_models, release_team

settings.register_profile("rollback-pr", max_examples=40, derandomize=True, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow,
                                                 HealthCheck.function_scoped_fixture])
settings.register_profile("rollback-nightly", max_examples=300, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow,
                                                 HealthCheck.function_scoped_fixture])
PROFILE = ("rollback-nightly" if os.environ.get("HYPOTHESIS_PROFILE") == "lean-nightly"
           else "rollback-pr")

pytestmark = pytest.mark.timeout(900)

PURE = {"run_tests": "pure"}
AGENTS = ("pm", "coder", "reviewer")
FINGERPRINT_DESYNC = "with different content"
BEFORE_FORK = "before its fork"


def interventions(kind: str, view: BoundaryView) -> list:
    """Interventions for a fork at ``view``'s boundary: aimed at its agent, at another one
    (RB1), or two producers at once (RB1, ``_SOLE``)."""
    if kind == "none" or view.agent is None:
        return []
    agent = view.agent
    if kind == "wrong":
        agent = next(a for a in AGENTS if a != view.agent)
    if view.boundary is Boundary.GENERATION:
        one = ReplaceGeneration(agent=agent, content="release in 5 min, ship it")
        return [one, ReplaceGeneration(agent=agent, content="again")] if kind == "two" else [one]
    if view.boundary is Boundary.BEFORE_ACTIVATION:
        return [StageInput(agent=agent, content="0 failed, ship it")]
    if view.boundary is Boundary.TOOL_CALL:
        return [BlockToolCall(agent=agent, message="blocked")]
    return [OverrideToolResult(agent=agent, result="0 failed, 15 passed")]


class Drawn(AuditPolicy):
    """Branch 0 may stage input below the fork and rolls back at the reviewer's generation;
    branch 1 may roll back once more; later branches only run."""

    name = "rollback-agreement"

    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan
        self.views: dict[str, list[BoundaryView]] = {}
        self.rolled: set[str] = set()

    def target(self, frac: float, view: BoundaryView) -> int:
        return max(1, min(view.revision, math.ceil(frac * view.revision)))

    async def decide(self, view: BoundaryView) -> list:
        seen = self.views.setdefault(view.branch, [])
        seen.append(view)
        plan, branch = self.plan, view.branch
        if branch == "branch-0" and (view.boundary, view.agent, view.turn) == (
                Boundary.BEFORE_ACTIVATION, "coder", 1) and plan["stage"] != "no":
            text = "VETO this" if plan["stage"] == "vetoed" else "CANARY"
            return [StageInput(agent="coder", content=text)]
        is_verdict = (view.boundary, view.agent) == (Boundary.GENERATION, "reviewer")
        if is_verdict and branch not in self.rolled:
            if branch == "branch-0":
                t = self.target(plan["frac0"], view)
                self.rolled.add(branch)
                return [Rollback(to_revision=t, interventions=interventions(plan["kind"], seen[t - 1]),
                                 reason="drawn")]
            if branch == "branch-1" and plan["frac1"] is not None:
                self.rolled.add(branch)
                return [Rollback(to_revision=self.target(plan["frac1"], view), reason="again")]
        return []


async def veto_marked(view: BoundaryView, op) -> str | None:
    return "vetoed" if "VETO" in str(getattr(op, "content", "")) else None


plans = st.fixed_dictionaries({
    "frac0": st.floats(0.05, 1.0), "kind": st.sampled_from(["own", "wrong", "two", "none"]),
    "stage": st.sampled_from(["no", "applied", "vetoed"]),
    "frac1": st.none() | st.floats(0.05, 1.0),
})


# --------------------------------------------------------------- checks
def applied(controller: AuditController, revision: int) -> list[tuple[str, Any]]:
    return [(r.op, r.payload) for r in controller.ledger.applied() if r.revision == revision]


def check_branch(lean, parent: AuditController, child: AuditController, node: dict,
                 parent_fork: int | None, t8: bool) -> list[str]:
    """Every disagreement between the child, its parent and the model. ``t8=False`` when a
    fault was injected, so property 1's premise does not hold and only agreement is checked."""
    replay = child.replay
    assert replay is not None
    fork, ivs = replay.fork_revision, list(replay.interventions)
    numbers: dict[str, int] = {}

    def fp(text: str) -> int:
        return numbers.setdefault(text, len(numbers))

    status, reason = node["status"], node["reason"]
    fingerprint_desync = status == BranchStatus.REPLAY_DESYNC.value and FINGERPRINT_DESYNC in reason
    natural = status == BranchStatus.COMPLETED.value or (
        status == BranchStatus.REPLAY_DESYNC.value and BEFORE_FORK in reason)
    seen = child.steps
    policy = [[] if s.revision < fork
              else list(s.decisions[len(ivs):]) if s.revision == fork else list(s.decisions)
              for s in seen]
    reply = lean.ask({
        "cmd": "branch", "fork": fork, "interventions": [lean_op(op) for op in ivs],
        "parent": [{"boundary": s.boundary.value, "fingerprint": fp(s.fingerprint),
                    "decisions": [lean_op(op) for op in s.decisions],
                    "over_budget": s.over_budget is not None} for s in replay.steps],
        "child": [{"fingerprint": fp(s.fingerprint), "policy": [lean_op(op) for op in p]}
                  for s, p in zip(seen, policy, strict=True)],
        "reached": child.revision})
    problems = []
    expected = [False] * len(seen)
    if fingerprint_desync:
        expected[-1] = True
    if reply["desync"] != expected:
        problems.append(f"desync: model {reply['desync']}, python {expected} ({reason})")
    for s, batch in zip(seen, reply["batches"], strict=True):
        if fingerprint_desync and s is seen[-1]:
            continue  # ended before deciding
        if batch != [op.name for op in s.decisions]:
            problems.append(f"revision {s.revision}: model batch {batch}, "
                            f"python {[op.name for op in s.decisions]}")
    if natural and reply["finished"] != status:
        problems.append(f"end: model {reply['finished']}, python {status} ({reason})")
    # Property 1 on a real run, unless the fork is past the parent's own (RB3).
    nested_above = parent_fork is not None and fork > parent_fork
    if t8 and not nested_above:
        if status == BranchStatus.REPLAY_DESYNC.value:
            problems.append(f"desynced although not nested above its parent's fork: {reason}")
        for s in seen:
            if s.revision < fork:
                if s.fingerprint != parent.steps[s.revision - 1].fingerprint:
                    problems.append(f"revision {s.revision}: fingerprint differs from parent")
                if applied(child, s.revision) != applied(parent, s.revision):
                    problems.append(f"revision {s.revision}: applied {applied(child, s.revision)}, "
                                    f"parent {applied(parent, s.revision)}")
    if status == BranchStatus.COMPLETED.value and child.revision < fork:
        problems.append(f"completed at revision {child.revision}, before its fork at {fork}")
    return problems


def check_audit(lean, log, controllers: list[AuditController], t8: bool = True) -> list[str]:
    tree = sample(log).store[BRANCHES_KEY]
    assert len(tree) == len(controllers)
    problems = []
    for i in range(1, len(controllers)):
        parent_replay = controllers[i - 1].replay
        problems += [f"{tree[i]['id']}: {p}" for p in check_branch(
            lean, controllers[i - 1], controllers[i], tree[i],
            parent_replay.fork_revision if parent_replay else None, t8)]
    return problems


# --------------------------------------------------------------- tests
@settings(settings.get_profile(PROFILE))
@given(plan=plans)
def test_drawn_rollbacks_agree_with_the_model(lean, tmp_path_factory, plan):
    policy = Drawn(plan)
    log, controllers = run_branches(tmp_path_factory.mktemp("drawn"), lambda: policy,
                                    tool_replay=PURE, approver=veto_marked)
    assert len(controllers) >= 2  # branch 0 always rolls back
    tree = sample(log).store[BRANCHES_KEY]
    event(f"statuses: {[b['status'] for b in tree]}")
    child = controllers[1]
    applied_at_fork = [r for r in child.ledger.applied() if r.revision == child.replay.fork_revision]
    event(f"interventions: {plan['kind']}, applied at the fork: {bool(applied_at_fork)}")
    event(f"below the fork in branch 0: {plan['stage']}")
    assert check_audit(lean, log, controllers) == []


def test_a_nested_rollback_past_the_parent_fork_desyncs_as_the_model_says(lean, tmp_path):
    """RB3 on a real run: the child of branch 1 forks past branch 1's own fork; a message
    posted between the two forks carries branch 1's ids, so the grandchild desyncs, and the
    model's fingerprint check agrees where."""
    policy = Drawn({"frac0": 0.3, "kind": "none", "stage": "no", "frac1": 0.9})
    log, controllers = run_branches(tmp_path, lambda: policy, tool_replay=PURE)
    tree = sample(log).store[BRANCHES_KEY]
    assert [b["status"] for b in tree] == ["rolled_back", "rolled_back", "replay_desync"]
    assert check_audit(lean, log, controllers) == []


NOT_PURE = {"calls": 0}


@tool(name="run_tests")
def flaky_tests() -> Tool:
    async def execute() -> str:
        """Run the project's test suite."""
        NOT_PURE["calls"] += 1  # state outside the store: declared pure, but not
        return f"{NOT_PURE['calls']} failed"

    return execute


def test_a_tool_that_is_not_pure_desyncs_where_the_model_says(lean, tmp_path):
    policy = Drawn({"frac0": 1.0, "kind": "own", "stage": "no", "frac1": None})
    log, controllers = run_branches(
        tmp_path, lambda: policy, tool_replay=PURE,
        tool_bindings={"run_tests": ToolFactory(lambda ctx: flaky_tests())})
    tree = sample(log).store[BRANCHES_KEY]
    assert tree[1]["status"] == "replay_desync" and FINGERPRINT_DESYNC in tree[1]["reason"]
    assert check_audit(lean, log, controllers, t8=False) == []


def test_a_child_that_halts_before_its_fork_ends_as_the_model_says(lean, tmp_path):
    """RB2: fault injection halts the child before its fork."""
    policy = Drawn({"frac0": 1.0, "kind": "own", "stage": "no", "frac1": None})
    controllers: list[AuditController] = []

    def halt_the_child(controller: AuditController) -> None:
        controllers.append(controller)
        if controller.branch == "branch-0":
            return
        before_turn = controller.before_turn

        async def halting(turn: int) -> None:
            await before_turn(turn)
            controller.runtime.halt("injected fault")

        controller.before_turn = halting

    task = audit_task(policy=lambda: policy, on_controller=halt_the_child, tool_replay=PURE,
                      **release_team())
    model = get_model("mockllm/model", custom_outputs=release_models, memoize=False)
    with id_scope(sequential_ids()):
        log = inspect_eval(task, model=model, log_dir=str(tmp_path), display="none")[0]
    tree = sample(log).store[BRANCHES_KEY]
    assert tree[1]["status"] == "replay_desync" and BEFORE_FORK in tree[1]["reason"]
    assert check_audit(lean, log, controllers, t8=False) == []
