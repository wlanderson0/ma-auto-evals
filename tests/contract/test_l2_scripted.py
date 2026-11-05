"""The ``scripted`` inner condition and the mid-run helper (L2 Task 5; spec §3.3, §4.1).

Runs built grid variants offline on the scripted mock models. The Advocate's model
in the brief is a live id, so these tests build it with no model of its own: it then
uses the task's (mock) model, like the other agents.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from inspect_ai import eval as inspect_eval
from orbit.ids import id_scope, sequential_ids

from experiments.l2.apply import apply, seat_scope
from experiments.l2.brief import parse_brief
from experiments.l2.inner import ScriptedHelper, scripted_policy, with_agent
from experiments.run import _mock_models, make_policy, run_condition
from experiments.scenarios import SCENARIOS, Activation
from orbit_audit.boundaries import Boundary
from orbit_audit.measurements import MEASUREMENTS_KEY
from orbit_audit.policy import AuditPolicy, ScriptedPolicy
from orbit_audit.task import audit_task

MILESTONE1 = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / "milestone1.md"
GRID = {v.id: v for v in parse_brief(MILESTONE1).variants}
MESSAGES = "CommunicationState:messages"


def offline(variant):
    """The variant with the Advocate's live model removed (it falls back to the mock)."""
    mutations = tuple(
        dataclasses.replace(m, args={**m.args, "model": None}) if m.op == "add_agent" else m
        for m in variant.mutations)
    return dataclasses.replace(variant, mutations=mutations)


def built(vid: str):
    variant = offline(GRID[vid])
    return apply(SCENARIOS[variant.base]({}), variant)


def run(tmp_path: Path, scenario, policy=None, *, succeed: bool = True):
    """One sample of ``scenario`` on the mock models: (sample, controllers, policy)."""
    controllers = []
    policy = policy or make_policy("scripted", scenario, None)
    task = audit_task(policy=lambda: policy, on_controller=controllers.append,
                      scope=scenario.scope, **scenario.task)
    with id_scope(sequential_ids()):
        log = inspect_eval(task, model=_mock_models()["target"],
                           log_dir=str(tmp_path / scenario.name), display="none")[0]
    if succeed:
        assert log.status == "success", log.samples[0].error
    return log.samples[0], controllers, policy


def posts(sample) -> list[dict]:
    return list(sample.store[MESSAGES])


def authors(sample) -> dict[str, str]:
    return sample.store[MEASUREMENTS_KEY]["branches"]["branch-0"]["authors"]


# ------------------------------------------------------- step 1: turn indexing
class Recorder(AuditPolicy):
    def __init__(self) -> None:
        self.views = []

    async def decide(self, view):
        self.views.append(view)
        return []


def test_between_turns_turn_is_zero_based_and_posts_share_it(tmp_path):
    """Pins the conversion ``Activation.after_turn`` relies on: the pause after the
    4th turn (end of round 1) has ``view.turn == 3``; posts carry the same index."""
    scenario = apply(SCENARIOS["mfa_rumor"]({}), dataclasses.replace(
        GRID["M4"], mutations=tuple(m for m in GRID["M4"].mutations
                                    if m.op == "set_rounds")))  # 2 rounds, nothing else
    assert len(scenario.task["execution"].plan.steps) == 8
    recorder = Recorder()
    sample, _, _ = run(tmp_path, scenario, recorder)
    pauses = [v for v in recorder.views if v.boundary == Boundary.BETWEEN_TURNS]
    assert [v.turn for v in pauses] == list(range(8))
    assert len(pauses[3].pending_steps) == 4  # after round 1: one round left
    by_turn = {v.turn: v.agent for v in recorder.views if v.boundary == Boundary.GENERATION}
    assert all(post["turn"] is not None for post in posts(sample))
    assert all(by_turn[post["turn"]] == post["sender"] for post in posts(sample))
    assert [post["turn"] // 4 for post in posts(sample)] == [0, 0, 0, 0, 1, 1, 1, 1]


# --------------------------------------------------------------- the opening
@pytest.mark.parametrize("vid", ["M0", "K1"])
def test_the_opening_is_scripted_then_the_seat_is_itself(tmp_path, vid):
    """L2-6: Alice's first post is exactly the opening, labelled auditor-as:alice; her
    later posts are her own model's, labelled agent:alice."""
    scenario = built(vid)
    sample, _, _ = run(tmp_path, scenario)
    alice = [p for p in posts(sample) if p["sender"] == "alice"]
    [(channel, text)] = scenario.opening
    assert (alice[0]["channel_id"], alice[0]["content"]) == (channel, text)
    labels = authors(sample)
    assert labels[alice[0]["id"]] == "auditor-as:alice"
    assert len(alice) == 3 and all(labels[p["id"]] == "agent:alice" for p in alice[1:])
    assert all(p["content"] != text for p in alice[1:])  # the mock's own words
    others = [p for p in posts(sample) if p["sender"] != "alice"]
    assert all(labels[p["id"]] == f"agent:{p['sender']}" for p in others)


def test_make_policy_builds_the_scripted_helper():
    scenario = built("M0")
    policy = make_policy("scripted", scenario, None)
    assert isinstance(policy, ScriptedHelper) and isinstance(policy, ScriptedPolicy)
    assert len(policy.rules) == 1
    assert len(scripted_policy(built("M7")).rules) == 2


# ------------------------------------------------------------ the helper's ops
def test_m7_helper_only_writes_the_opening_and_one_amendment(tmp_path):
    """L2-3b, on the helper's actual output."""
    sample, [controller], _ = run(tmp_path, built("M7"))
    applied = [r for r in controller.ledger.applied() if not r.replayed]
    assert [(r.op, r.agents) for r in applied] == [
        ("replace_generation", ("alice",)),
        ("amend_pending", ("alice", "bob", "carol", "dave", "erin"))]
    opening, amend = applied
    assert opening.turn == 0 and opening.boundary == Boundary.GENERATION
    assert amend.turn == 3 and amend.boundary == Boundary.BETWEEN_TURNS  # after round 1
    round_ = [["alice"], ["erin"], ["bob"], ["carol"], ["dave"]]
    assert amend.payload["steps"] == round_ * 2
    assert not controller.ledger.rejected()


def test_a_refused_helper_op_errors_the_sample(tmp_path):
    """M7 with the seat-only scope would silently become M6; it must fail instead."""
    narrow = dataclasses.replace(built("M7"), scope=seat_scope("alice"))
    sample, _, _ = run(tmp_path, narrow, succeed=False)
    assert sample.error is not None and "amend_pending" in sample.error.message
    assert "refused" in sample.error.message


@pytest.mark.parametrize("vid, applied", [
    ("M7", {"replace_generation": 1, "amend_pending": 1}), ("K1", {"replace_generation": 1})])
def test_variants_run_through_the_harness(tmp_path, vid, applied):
    """The production path (limits, tool replay, scorers): complete scores, the helper's
    ops only, M7's amendment counted as off-seat (results.json is not read by L2)."""
    models = _mock_models()
    models["probe"] = models["judge"]
    [row] = run_condition(built(vid), "scripted", 1, tmp_path, models)
    assert row["status"] == "success" and row["error"] is None
    assert "incomplete" not in row
    assert row["interventions"]["applied"] == applied
    assert row["interventions"]["rejected"] == []
    assert row["interventions"]["off_seat"] == (["amend_pending"] if vid == "M7" else [])


def generation_order(controller) -> list[str]:
    return [trace.agent for trace in controller.generations]


def test_m6_and_m7_differ_only_in_the_activation(tmp_path):
    """L2-8: same setup and helper; the Advocate never acts in M6, and in M7 acts in
    rounds 2 and 3 only, right after Alice each time."""
    m6, m7 = built("M6"), built("M7")
    assert dataclasses.replace(m7, name=m6.name, task={**m7.task, "name": m6.name},
                               activations=(), variant=m6.variant) == m6
    assert m7.activations == (Activation("erin", 4, "after:alice"),)
    p6, p7 = make_policy("scripted", m6, None), make_policy("scripted", m7, None)
    assert type(p6) is type(p7) and m6.scope == m7.scope
    _, [c6], _ = run(tmp_path, m6)
    _, [c7], _ = run(tmp_path, m7)
    round_ = ["alice", "bob", "carol", "dave"]
    assert generation_order(c6) == round_ * 3
    assert generation_order(c7) == round_ + ["alice", "erin", "bob", "carol", "dave"] * 2


def test_an_idle_advocate_changes_nothing_else(tmp_path):
    """M0 and M6 differ only by the never-scheduled Advocate (and the helper scope):
    same posts, and the same prompts for every other agent."""
    s0, [c0], _ = run(tmp_path, built("M0"))
    s6, [c6], _ = run(tmp_path, built("M6"))

    def said(sample):
        return [(p["sender"], p["channel_id"], p["content"]) for p in posts(sample)]

    def seen(controller):
        # Orbit's message ids are derived from each message's delivery (the Advocate is
        # one more reader of #town), so they differ; every word an agent reads must not.
        def text(message):
            return re.sub(r"message-[0-9a-f]{16,}", "message-?", message.text)

        return [(t.agent, [(m.role, text(m)) for m in t.input]) for t in controller.generations]

    assert said(s0) == said(s6)
    assert seen(c0) == seen(c6)


# ------------------------------------------------------------------- helpers
def test_with_agent_inserts_in_every_remaining_round():
    pending = (("bob",), ("alice",), ("bob",), ("alice",), ("bob",))
    assert with_agent(pending, Activation("erin", 1, "after:alice")) == (
        ("bob",), ("alice",), ("erin",), ("bob",), ("alice",), ("erin",), ("bob",))
    assert with_agent(pending, Activation("erin", 1, "before:alice")) == (
        ("bob",), ("erin",), ("alice",), ("bob",), ("erin",), ("alice",), ("bob",))
    with pytest.raises(ValueError, match="not in the pending schedule"):
        with_agent(pending, Activation("erin", 1, "after:dave"))
