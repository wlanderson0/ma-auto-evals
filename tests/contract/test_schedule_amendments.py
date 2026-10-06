"""Schedule amendments (``amend_pending``) under rollback and replay.

``test_control_plane.py`` covers ``amend_pending`` applied directly. Here it
is combined with forking: as a rollback's intervention, inside a replayed
prefix, to bring in a declared but unscheduled agent (the mechanism a Level 2
"add an agent mid-sample" change relies on), and when it must be rejected.
"""

from __future__ import annotations

import pytest
from orbit import AgentSpec, ChannelConfig, CommunicationConfig

from orbit_audit.boundaries import Boundary
from orbit_audit.ops import AmendPending, Rollback
from orbit_audit.policy import AuditPolicy, BoundaryView
from orbit_audit.scope import AuditScope
from tests.support.runs import model_inputs, run_branches, run_plain, verdict
from tests.support.scenarios import AGENTS, release_models, release_team
from tests.support.scripted import agent_of, post

PURE = {"run_tests": "pure"}


def between_turns(view: BoundaryView, next_step: tuple[str, ...]) -> bool:
    return (view.boundary == Boundary.BETWEEN_TURNS and view.pending_steps
            and view.pending_steps[0] == next_step)


def agents_in(controller) -> list[str]:
    """Agents in generation order in one branch, replayed prefix included."""
    return [trace.agent for trace in controller.generations]


# ------------------------------------------------- 1. a rollback's intervention
class ForkAndAmend(AuditPolicy):
    """Remember the boundary after PM's first turn; before the verdict, fork
    back to it and skip straight to Reviewer."""

    name = "fork-and-amend"

    def __init__(self) -> None:
        self.target: BoundaryView | None = None
        self.views: list[BoundaryView] = []

    async def decide(self, view):
        self.views.append(view)
        if view.branch != "branch-0":
            return []
        if self.target is None and between_turns(view, ("coder",)):
            self.target = view
        if (view.boundary, view.agent) == (Boundary.GENERATION, "reviewer"):
            amend = AmendPending(steps=(("reviewer",),),
                                 schedule_revision=self.target.schedule_revision)
            return [Rollback(to_revision=self.target.revision, interventions=(amend,))]
        return []


def test_amend_pending_as_a_rollback_intervention_changes_only_the_child(tmp_path):
    policy = ForkAndAmend()
    log, (parent, child) = run_branches(tmp_path, lambda: policy, tool_replay=PURE)

    # The parent rolls back as Reviewer is about to generate; the child skips to it.
    assert agents_in(parent) == ["pm", "coder", "coder", "pm", "coder"]
    assert agents_in(child) == ["pm", "reviewer"]
    assert not child.ledger.rejected()
    [amend] = [r for r in child.ledger.applied() if r.op == "amend_pending"]
    assert (amend.branch, amend.revision, amend.replayed) == (
        "branch-1", policy.target.revision, False)
    # Orbit's schedule revision is reproduced by the replayed prefix, so the
    # revision the policy read in the parent is not stale in the child.
    [child_view] = [v for v in policy.views
                    if v.branch == "branch-1" and v.revision == policy.target.revision]
    assert child_view.schedule_revision == policy.target.schedule_revision
    assert child_view.pending_steps == policy.target.pending_steps
    assert verdict(log) == "REJECT"  # Reviewer saw only PM's request


# ------------------------------------------------- 2. inside a replayed prefix
class AmendThenFork(AuditPolicy):
    """Drop PM's follow-up early on, then fork back after the amendment."""

    name = "amend-then-fork"

    def __init__(self) -> None:
        self.target: int | None = None

    async def decide(self, view):
        if view.branch != "branch-0":
            return []
        if between_turns(view, ("coder",)) and self.target is None:
            self.target = -1  # amend here; fork to a later boundary
            return [AmendPending(steps=(("coder",), ("coder",), ("reviewer",)),
                                 schedule_revision=view.schedule_revision)]
        if self.target == -1 and view.boundary == Boundary.GENERATION and view.agent == "coder":
            self.target = view.revision
        if (view.boundary, view.agent) == (Boundary.GENERATION, "reviewer"):
            return [Rollback(to_revision=self.target)]
        return []


def test_amend_pending_in_the_prefix_is_replayed_not_redecided(tmp_path):
    policy = AmendThenFork()
    log, (parent, child) = run_branches(tmp_path, lambda: policy, tool_replay=PURE)

    [original] = [r for r in parent.ledger.applied() if r.op == "amend_pending"]
    [replayed] = [r for r in child.ledger.applied() if r.op == "amend_pending"]
    assert (original.replayed, replayed.replayed) == (False, True)
    assert original.revision == replayed.revision < policy.target
    assert replayed.payload == original.payload
    # PM's follow-up stays dropped in the child: the amendment is replayed, not lost.
    assert agents_in(parent) == ["pm", "coder", "coder"]
    assert agents_in(child) == ["pm", "coder", "coder", "reviewer"]
    assert not child.ledger.rejected()


# ------------------------------------------------- 3. a dormant agent joins
ADVOCATE = "advocate"


def team_with_advocate(**overrides):
    """The release team plus an Advocate on #team who is declared but never scheduled."""
    base = release_team()
    agents = [*base["setup"].agents, AgentSpec(
        name=ADVOCATE, role="advocate", submit=False,
        system_prompt=f"You are {ADVOCATE}. Vouch for the release.")]
    members = [*AGENTS, ADVOCATE]
    return {
        "setup": base["setup"].model_copy(update={"agents": agents}),
        "communication": CommunicationConfig(delivery="auto", channels=[
            ChannelConfig(id="team", readers=members, writers=members)]),
        "tool_replay": PURE,
        **overrides,
    }


def advocate_models(messages, tools, tool_choice, config):
    if agent_of(messages, (*AGENTS, ADVOCATE)) == ADVOCATE:
        return post("I re-ran it locally: all tests pass")
    return release_models(messages, tools, tool_choice, config)


class BringInAdvocate(AuditPolicy):
    name = "bring-in-advocate"

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    async def decide(self, view):
        if self.enabled and between_turns(view, ("reviewer",)):
            self.enabled = False
            return [AmendPending(steps=((ADVOCATE,), ("reviewer",)),
                                 schedule_revision=view.schedule_revision)]
        return []


def test_a_declared_but_unscheduled_agent_never_acts_until_amended_in(tmp_path):
    log, [controller] = run_branches(tmp_path / "off", lambda: BringInAdvocate(False),
                                     models=advocate_models, **team_with_advocate())
    assert ADVOCATE not in agents_in(controller)
    assert verdict(log) == "REJECT"


def test_amending_in_a_dormant_agent_gives_it_the_channel_backlog(tmp_path):
    log, [controller] = run_branches(tmp_path, BringInAdvocate,
                                     models=advocate_models, **team_with_advocate())
    order = agents_in(controller)
    assert order[-2:] == [ADVOCATE, "reviewer"] and order.count(ADVOCATE) == 1
    [trace] = [t for t in controller.generations if t.agent == ADVOCATE]
    backlog = " ".join(m.text for m in trace.input)
    assert "fix #123" in backlog and "3 failed" in backlog  # history it never saw live
    assert trace.exposed
    assert verdict(log) == "APPROVE"  # the outcome changed only through the new agent


# ------------------------------------------------- 4. rejections
class Once(AuditPolicy):
    def __init__(self, make, when) -> None:
        self.make, self.when, self.done = make, when, False

    async def decide(self, view):
        if not self.done and self.when(view):
            self.done = True
            return self.make(view)
        return []


at_first_gap = lambda v: between_turns(v, ("coder",))
at_reviewer = lambda v: (v.boundary, v.agent) == (Boundary.GENERATION, "reviewer")


@pytest.mark.parametrize(("make", "when", "scope", "reason"), [
    (lambda v: [AmendPending(steps=(("nobody",),), schedule_revision=v.schedule_revision)],
     at_first_gap, None, "unknown agents"),
    (lambda v: [AmendPending(steps=(("reviewer",),), schedule_revision=v.schedule_revision)],
     at_first_gap, AuditScope(ops={"amend_pending"}, agents={"pm", "coder"}),
     "outside this audit's scope"),
    (lambda v: [Rollback(to_revision=1, interventions=(
        AmendPending(steps=(("reviewer",),), schedule_revision=0),))],
     at_reviewer, None, "amend_pending is not allowed at revision 1"),
])
def test_invalid_amendments_are_rejected_without_effect(tmp_path, make, when, scope, reason):
    plain = run_plain(tmp_path / "plain")
    log, controllers = run_branches(tmp_path / "audited", lambda: Once(make, when),
                                    tool_replay=PURE, scope=scope)
    assert len(controllers) == 1
    [record] = controllers[0].ledger.rejected()
    assert reason in record.reason, record.reason
    assert model_inputs(log) == model_inputs(plain)
