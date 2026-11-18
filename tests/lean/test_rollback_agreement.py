"""The rollback model against real branches (rollback plan step 5, ``lean/rollback-map.md``).

Scripted release-team audits with Hypothesis-drawn rollbacks:
- where each lands;
- which interventions it carries: well-targeted, aimed at another agent (RB1), or two
  producers at once (RB1);
- an applied or vetoed op below the fork;
- whether the woken policy adds an op at the child's fork;
- an intervention budget;
- an approver that would veto a replayed op if asked during replay;
- a second rollback in the child, within or past its own fork (RB3).

For every child branch the test checks four things.

* **Construction.** The child's replay is the rollback its parent applied: the fork
  revision and the interventions.
* **Model agreement.** The Lean driver is given the parent's recorded steps and, for each
  child boundary, its fingerprint and what the policy *returned* there (recorded by the
  policy, independently of what the controller decided). It computes, with `Branch.lean`'s
  own functions:
  - the batch decided on (`batchAt`);
  - whether the fingerprint check ends the branch (`desynced`);
  - how a run that ends on its own is classified (`finished`, RB2).

  These must match what the Python did.
* **Fidelity on real runs** (property 1, empirically for Orbit and the scripted models).
  Below the fork, and up to its parent's own fork when it is nested past it, the child has
  its parent's fingerprints and applies its parent's ops. Unless nested past, it never
  desyncs. The policy is first asked at the fork. A completed child reached its fork.
* **Bookkeeping.** The intervention budget counts live counted ops only, and a message id
  shared by two branches names the same message.

Fault injection exercises what scripted runs never do on their own: a tool that is not
really pure (a desync, also exactly at the fork) and a run that halts before its fork
(RB2). Python mutants of the replay path (over-budget verdict recomputed, approver asked
during replay) must be caught.
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

from orbit_audit import controller as controller_module
from orbit_audit.boundaries import Boundary
from orbit_audit.branches import AuditLimits, BranchStatus
from orbit_audit.controller import AuditController
from orbit_audit.ops import (
    BlockToolCall,
    InjectMessage,
    OverrideToolResult,
    ReplaceGeneration,
    Rollback,
    StageInput,
)
from orbit_audit.policy import AuditPolicy, BoundaryView
from orbit_audit.runner import BRANCHES_KEY, BUDGET_KEY
from orbit_audit.task import audit_task
from tests.lean.driver import lean_op
from tests.support.runs import run_branches, sample
from tests.support.scenarios import release_models, release_team

settings.register_profile("rollback-pr", max_examples=40, derandomize=True, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("rollback-nightly", max_examples=120, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
PROFILE = ("rollback-nightly" if os.environ.get("HYPOTHESIS_PROFILE") == "lean-nightly"
           else "rollback-pr")

pytestmark = pytest.mark.timeout(900)

PURE = {"run_tests": "pure"}
AGENTS = ("pm", "coder", "reviewer")
FINGERPRINT_DESYNC = "with different content"
BEFORE_FORK = "before its fork"
FYI = InjectMessage(channel_id="team", sender="pm", recipients=["coder"], content="FYI")
UNCOUNTED = {"halt", "rollback"}


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
    """Branch 0 may stage input below the fork and rolls back at the reviewer's generation
    (or to its first tool result); branch 1 may roll back once more; a child may add an op
    when woken at its fork. Records what it returned at each boundary."""

    name = "rollback-agreement"

    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan
        self.views: dict[str, list[BoundaryView]] = {}
        self.returned: dict[tuple[str, int], list] = {}
        self.rolled: set[str] = set()

    def target(self, frac: float, seen: list[BoundaryView], view: BoundaryView) -> int:
        if self.plan.get("at") == "tool_result":
            return next(v.revision for v in seen
                        if v.boundary is Boundary.TOOL_RESULT and v.tool_name == "run_tests")
        return max(1, min(view.revision, math.ceil(frac * view.revision)))

    async def decide(self, view: BoundaryView) -> list:
        ops = self.choose(view)
        self.returned[(view.branch, view.revision)] = list(ops)
        return ops

    def choose(self, view: BoundaryView) -> list:
        seen = self.views.setdefault(view.branch, [])
        seen.append(view)
        plan, branch = self.plan, view.branch
        if branch != "branch-0" and len(seen) == 1 and plan.get("woken"):
            return [FYI]  # the woken policy's own op at the fork
        if branch == "branch-0" and (view.boundary, view.agent, view.turn) == (
                Boundary.BEFORE_ACTIVATION, "coder", 1) and plan["stage"] != "no":
            text = "VETO this" if plan["stage"] == "vetoed" else "CANARY"
            return [StageInput(agent="coder", content=text)]
        is_verdict = (view.boundary, view.agent) == (Boundary.GENERATION, "reviewer")
        if is_verdict and branch not in self.rolled:
            if branch == "branch-0":
                t = self.target(plan["frac0"], seen, view)
                self.rolled.add(branch)
                return [Rollback(to_revision=t, interventions=interventions(plan["kind"], seen[t - 1]),
                                 reason="drawn")]
            if branch == "branch-1" and plan["frac1"] is not None:
                self.rolled.add(branch)
                return [Rollback(to_revision=self.target(plan["frac1"], seen, view), reason="again")]
        return []


async def approver(view: BoundaryView, op) -> str | None:
    """Vetoes "VETO"; would veto the replayed "CANARY" in a child if asked during replay
    (a stand-in for a stateful approver)."""
    content = str(getattr(op, "content", ""))
    if "VETO" in content or ("CANARY" in content and view.branch != "branch-0"):
        return "vetoed"
    return None


plans = st.fixed_dictionaries({
    "frac0": st.floats(0.05, 1.0), "kind": st.sampled_from(["own", "wrong", "two", "none"]),
    "stage": st.sampled_from(["no", "applied", "vetoed"]),
    "frac1": st.none() | st.floats(0.05, 1.0), "woken": st.booleans(),
    "budget": st.sampled_from([None, 1]),
})


def run_plan(tmp_path, plan: dict[str, Any], **overrides):
    policy = Drawn(plan)
    limits = AuditLimits(max_interventions=plan["budget"]) if plan.get("budget") else None
    log, controllers = run_branches(tmp_path, lambda: policy, tool_replay=PURE, approver=approver,
                                    limits=limits, **overrides)
    return log, controllers, policy


# --------------------------------------------------------------- checks
def applied(controller: AuditController, revision: int) -> list[tuple[str, Any]]:
    return [(r.op, r.payload) for r in controller.ledger.applied() if r.revision == revision]


def check_branch(lean, parent: AuditController, child: AuditController, node: dict,
                 parent_fork: int | None, policy: Drawn, t8: bool) -> list[str]:
    """Every disagreement between the child, its parent, the policy and the model.
    ``t8=False`` when a fault was injected: property 1's premise does not hold, so only
    agreement with the model is checked."""
    replay = child.replay
    assert replay is not None and replay.parent == parent.branch and node["id"] == child.branch
    fork, ivs = replay.fork_revision, list(replay.interventions)
    problems = []
    # construction: the child replays the rollback its parent applied
    [rollback] = [r for r in parent.ledger.applied() if r.op == "rollback"]
    if (rollback.payload["to_revision"], rollback.payload["interventions"]) != (
            fork, [{"op": op.name, **op.payload()} for op in ivs]):
        problems.append(f"replay is not the applied rollback: {rollback.payload}")
    numbers: dict[str, int] = {}

    def fp(text: str) -> int:
        return numbers.setdefault(text, len(numbers))

    status, reason = node["status"], node["reason"]
    fingerprint_desync = status == BranchStatus.REPLAY_DESYNC.value and FINGERPRINT_DESYNC in reason
    natural = status == BranchStatus.COMPLETED.value or (
        status == BranchStatus.REPLAY_DESYNC.value and BEFORE_FORK in reason)
    seen = child.steps
    returned = [policy.returned.get((child.branch, s.revision), []) for s in seen]
    reply = lean.ask({
        "cmd": "branch", "fork": fork, "interventions": [lean_op(op) for op in ivs],
        "parent": [{"boundary": s.boundary.value, "fingerprint": fp(s.fingerprint),
                    "decisions": [lean_op(op) for op in s.decisions],
                    "over_budget": s.over_budget is not None} for s in replay.steps],
        "child": [{"fingerprint": fp(s.fingerprint), "policy": [lean_op(op) for op in p]}
                  for s, p in zip(seen, returned, strict=True)],
        "reached": child.revision})
    expected = [False] * len(seen)
    if fingerprint_desync:
        expected[-1] = True
    if reply["desync"] != expected:
        problems.append(f"desync: model {reply['desync']}, python {expected} ({reason})")
    vetoed = {r.revision for r in child.ledger.rejected() if r.reason == "vetoed"}
    for s, batch in zip(seen, reply["batches"], strict=True):
        if fingerprint_desync and s is seen[-1]:
            continue  # ended before deciding
        python = [op.name for op in s.decisions]
        model = [] if s.revision in vetoed else batch  # a vetoed batch records nothing (M1)
        if model != python:
            problems.append(f"revision {s.revision}: model batch {model}, python {python}")
        if s.revision == fork and [lean_op(op) for op in s.decisions[:len(ivs)]] != [
                lean_op(op) for op in ivs] and s.revision not in vetoed:
            problems.append("fork: the batch does not start with the interventions")
    if natural and reply["finished"] != status:
        problems.append(f"end: model {reply['finished']}, python {status} ({reason})")
    # property 1 on a real run: fully unless nested past the parent's fork (RB3), and up to
    # the parent's fork when nested
    nested_above = parent_fork is not None and fork > parent_fork
    same_up_to = min(fork - 1, parent_fork) if nested_above else fork - 1
    if t8:
        if not nested_above and status == BranchStatus.REPLAY_DESYNC.value:
            problems.append(f"desynced although not nested above its parent's fork: {reason}")
        for s in seen:
            if s.revision <= same_up_to:
                if s.fingerprint != parent.steps[s.revision - 1].fingerprint:
                    problems.append(f"revision {s.revision}: fingerprint differs from parent")
                if applied(child, s.revision) != applied(parent, s.revision):
                    problems.append(f"revision {s.revision}: applied {applied(child, s.revision)}, "
                                    f"parent {applied(parent, s.revision)}")
        asked = [v.revision for v in policy.views.get(child.branch, [])]
        if child.revision >= fork and not fingerprint_desync and asked and min(asked) != fork:
            problems.append(f"policy first asked at {min(asked)}, fork at {fork}")
    if status == BranchStatus.COMPLETED.value and child.revision < fork:
        problems.append(f"completed at revision {child.revision}, before its fork at {fork}")
    return problems


def check_audit(lean, log, controllers: list[AuditController], policy: Drawn,
                t8: bool = True) -> list[str]:
    store = sample(log).store
    tree = store[BRANCHES_KEY]
    assert len(tree) == len(controllers)
    problems = []
    for i in range(1, len(controllers)):
        parent_replay = controllers[i - 1].replay
        problems += [f"{tree[i]['id']}: {p}" for p in check_branch(
            lean, controllers[i - 1], controllers[i], tree[i],
            parent_replay.fork_revision if parent_replay else None, policy, t8)]
    # the budget counts live counted ops only (B1, R3)
    live = sum(1 for c in controllers for r in c.ledger.applied()
               if not r.replayed and r.op not in UNCOUNTED)
    if store[BUDGET_KEY]["interventions"] != live:
        problems.append(f"budget counted {store[BUDGET_KEY]['interventions']}, live ops {live}")
    # a message id shared by two branches names the same message (ids are the branch's own
    # after its fork, property 6)
    by_id: dict[str, tuple] = {}
    for node in tree:
        for m in node["messages"]:
            key = (m["sender"], m["content"], tuple(m["recipients"]), m["channel_id"])
            if by_id.setdefault(m["id"], key) != key:
                problems.append(f"{node['id']}: id {m['id']} names another branch's message")
    return problems


def fork_outcome(controllers: list[AuditController], plan: dict[str, Any],
                 policy: Drawn) -> list[str]:
    """RB1 on real runs: what happened to the first rollback's interventions at the fork."""
    child = controllers[1]
    fork, ivs = child.replay.fork_revision, list(child.replay.interventions)
    if not ivs or child.revision < fork or plan.get("budget"):
        return []
    names = [r.op for r in child.ledger.applied()
             if r.revision == fork and r.op != "inject_message"]  # not the woken policy's FYI
    rejected = [r.reason for r in child.ledger.rejected() if r.revision == fork]
    if any(isinstance(op, Rollback) for op in policy.returned.get((child.branch, fork), [])):
        # RB1: the woken policy rolls back again at the fork, so nothing in the batch applies
        if names or not any("only operation" in r for r in rejected):
            return [f"a rollback at the fork did not reject the batch: {names} {rejected}"]
        return []
    if plan["kind"] == "own" and names[:len(ivs)] != [op.name for op in ivs]:
        return [f"well-targeted interventions not applied at the fork: {rejected}"]
    if plan["kind"] == "wrong" and (names or not any("targets" in r for r in rejected)):
        return [f"interventions aimed at another agent not rejected as such: {names} {rejected}"]
    if plan["kind"] == "two" and len(ivs) == 2 and (
            names or not any("at most one" in r for r in rejected)):
        return [f"two producers not rejected as such: {names} {rejected}"]
    return []


# --------------------------------------------------------------- tests
@settings(settings.get_profile(PROFILE))
@given(plan=plans)
def test_drawn_rollbacks_agree_with_the_model(lean, tmp_path_factory, plan):
    log, controllers, policy = run_plan(tmp_path_factory.mktemp("drawn"), plan)
    tree = sample(log).store[BRANCHES_KEY]
    event(f"statuses: {[b['status'] for b in tree]}")
    if len(controllers) == 1:  # the rollback's interventions did not fit the budget
        [rejected] = [r for r in controllers[0].ledger.rejected() if r.op == "rollback"]
        assert plan["budget"] and "budget" in rejected.reason
        assert check_audit(lean, log, controllers, policy) == []
        return
    child = controllers[1]
    at_fork = [r for r in child.ledger.applied() if r.revision == child.replay.fork_revision]
    event(f"interventions: {plan['kind']}, applied at the fork: {bool(at_fork)}")
    event(f"below the fork in branch 0: {plan['stage']}; budget {plan['budget']}")
    assert check_audit(lean, log, controllers, policy) + fork_outcome(controllers, plan,
                                                                       policy) == []


def test_a_vetoed_batch_is_not_replayed(lean, tmp_path):
    """M1: the approver vetoes branch 0's staged input; branch 0 records no decisions there,
    and the child replays none."""
    plan = {"frac0": 1.0, "kind": "none", "stage": "vetoed", "frac1": None, "woken": False,
            "budget": None}
    log, controllers, policy = run_plan(tmp_path, plan)
    parent, child = controllers
    [veto] = [r for r in parent.ledger.rejected() if r.reason == "vetoed"]
    assert parent.steps[veto.revision - 1].decisions == ()
    assert child.steps[veto.revision - 1].decisions == ()
    assert check_audit(lean, log, controllers, policy) == []


def test_a_nested_rollback_past_the_parent_fork_desyncs_as_the_model_says(lean, tmp_path):
    """RB3 on a real run: the child of branch 1 forks past branch 1's own fork. A message
    posted between the two forks carries branch 1's ids, so the grandchild desyncs, and the
    model's fingerprint check agrees on where."""
    plan = {"frac0": 0.3, "kind": "none", "stage": "no", "frac1": 0.9, "woken": False,
            "budget": None}
    log, controllers, policy = run_plan(tmp_path, plan)
    tree = sample(log).store[BRANCHES_KEY]
    assert [b["status"] for b in tree] == ["rolled_back", "rolled_back", "replay_desync"]
    assert check_audit(lean, log, controllers, policy) == []


NOT_PURE = {"calls": 0}


@tool(name="run_tests")
def flaky_tests() -> Tool:
    async def execute() -> str:
        """Run the project's test suite."""
        NOT_PURE["calls"] += 1  # state outside the store: declared pure, but not
        return f"{NOT_PURE['calls']} failed"

    return execute


FLAKY = {"run_tests": ToolFactory(lambda ctx: flaky_tests())}


@pytest.mark.parametrize("at", [None, "tool_result"])
def test_a_tool_that_is_not_pure_desyncs_where_the_model_says(lean, tmp_path, at):
    """A desync below the fork, and (``at="tool_result"``) exactly at it: the first boundary
    whose fingerprint differs is the fork itself."""
    plan = {"frac0": 1.0, "kind": "own", "stage": "no", "frac1": None, "woken": False,
            "budget": None, "at": at}
    log, controllers, policy = run_plan(tmp_path, plan, tool_bindings=FLAKY)
    tree = sample(log).store[BRANCHES_KEY]
    assert tree[1]["status"] == "replay_desync" and FINGERPRINT_DESYNC in tree[1]["reason"]
    if at:
        assert controllers[1].revision == controllers[1].replay.fork_revision
    assert check_audit(lean, log, controllers, policy, t8=False) == []


def test_a_child_that_halts_before_its_fork_ends_as_the_model_says(lean, tmp_path):
    """RB2: fault injection halts the child before its fork."""
    plan = {"frac0": 1.0, "kind": "own", "stage": "no", "frac1": None, "woken": False,
            "budget": None}
    policy = Drawn(plan)
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
                      approver=approver, **release_team())
    model = get_model("mockllm/model", custom_outputs=release_models, memoize=False)
    with id_scope(sequential_ids()):
        log = inspect_eval(task, model=model, log_dir=str(tmp_path), display="none")[0]
    tree = sample(log).store[BRANCHES_KEY]
    assert tree[1]["status"] == "replay_desync" and BEFORE_FORK in tree[1]["reason"]
    assert check_audit(lean, log, controllers, policy, t8=False) == []


# --------------------------------------------------------------- mutants
def recompute_over_budget(self, view, ops):
    """Mutant: a replayed batch gets today's verdict instead of its parent's."""
    step = self.steps[view.revision - 1]
    step.over_budget = self.budget.intervention_problem(
        sum(map(controller_module._counted, ops)))
    return step.over_budget


async def veto_during_replay(self, view, ops):
    """Mutant: the approver is asked during replay too."""
    if self.approver is None:
        return {}
    for op in ops:
        if isinstance(op, controller_module._VETTED) and (reason := await self.approver(view, op)):
            return {id(op): reason}
    return {}


@pytest.mark.parametrize("name, mutant, plan", [
    ("_over_budget", recompute_over_budget,
     {"frac0": 1.0, "kind": "none", "stage": "applied", "frac1": None, "woken": False,
      "budget": 1}),
    ("_veto", veto_during_replay,
     {"frac0": 1.0, "kind": "none", "stage": "applied", "frac1": None, "woken": False,
      "budget": None}),
])
def test_replay_mutants_are_caught(lean, tmp_path, monkeypatch, name, mutant, plan):
    log, controllers, policy = run_plan(tmp_path / "plain", plan)
    assert check_audit(lean, log, controllers, policy) == []
    monkeypatch.setattr(AuditController, name, mutant)
    log, controllers, policy = run_plan(tmp_path / "mutant", plan)
    assert check_audit(lean, log, controllers, policy) != []
