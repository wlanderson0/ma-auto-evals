"""Phased rounds as setup inputs (``team_setup(phased=...)``; spec
``docs/level2-interface.md`` §3.5): the checks on a phased team, its steps, how L2
describes it, and the changes L2 cannot make to it. The game itself runs in
``tests/contract/test_phased_rounds.py``."""

from __future__ import annotations

import dataclasses

import pytest
from orbit import ChannelConfig

from experiments.l2.apply import ApplyError, activation
from experiments.l2.describe import schedule
from experiments.l2.model import Mutation
from experiments.scenarios import (
    Phase,
    RoundResult,
    Scenario,
    build_team,
    phased_rounds,
    step_phase,
    team_setup,
)

ROSTER = ("alice", "bob", "referee")
TABLE = (ChannelConfig(id="table", readers=list(ROSTER), writers=list(ROSTER)),)
PHASES = (Phase("talk"), Phase("propose", agents=("bob", "alice")), Phase("vote"))


def no_result(n: int) -> RoundResult:
    return RoundResult()


def game(phases=PHASES, referee="referee", channel="table", channels=TABLE, **changes):
    return team_setup({}, personas={a: a for a in ROSTER}, roster=ROSTER,
                      round_order=("alice", "bob"), channels=channels, output_agent="bob",
                      **{"rounds": 2, **changes},
                      phased=phased_rounds(phases, no_result, referee=referee,
                                           channel=channel))


def test_each_round_runs_its_phases_in_order_as_steps():
    team = game()
    assert team.plan_steps() == [
        {"id": f"r{n}-{phase}", "agents": agents} for n in (1, 2)
        for phase, agents in (("talk", ["alice", "bob"]), ("propose", ["bob", "alice"]),
                              ("vote", ["alice", "bob"]))]
    assert team.turns_per_round() == 3
    assert len(team.steps()) == 6


def test_changing_the_rounds_keeps_the_phases():
    team = dataclasses.replace(game(), rounds=3)
    assert [step["id"] for step in team.plan_steps()][-1] == "r3-vote"


def test_step_ids_give_the_round_and_phase():
    assert step_phase("r12-vote") == (12, "vote")
    assert step_phase("step-3") is None
    assert step_phase("r0-vote") is None


@pytest.mark.parametrize("changes, error", [
    (dict(phases=()), "at least one phase"),
    (dict(phases=(Phase("talk"), Phase("talk"))), "must differ"),
    (dict(phases=(Phase("Vote-2"),)), "lower-case words"),
    (dict(phases=(Phase("talk", agents=("carol",)),)), "not in the roster"),
    (dict(referee="alice"), "act in no phase"),
    (dict(referee="carol"), "must be in the roster"),
    (dict(channel="town"), "is not a channel"),
    (dict(channels=(ChannelConfig(id="table", readers=list(ROSTER),
                                  writers=["alice", "bob"]),)), "cannot write"),
    (dict(rounds=None, schedule=(("alice",), ("bob",))), "need rounds"),
])
def test_a_phased_team_is_checked(changes, error):
    with pytest.raises(ValueError, match=error):
        game(**changes)


def test_l2_describes_the_phases():
    team = game()
    scenario = Scenario(name="game", property="", seat=None, targets=("alice", "bob"),
                        task=build_team("game", team), goal="", probes={}, judge="j",
                        team=team)
    assert schedule(scenario).startswith(
        "2 rounds, each in phases: talk (alice, bob); propose (bob, alice); vote (alice, "
        "bob), then the round is resolved and referee posts on table. Agents with no "
        "turns: referee.")


def test_activate_agent_is_refused_for_phased_rounds():
    mutation = Mutation(axis="schedule", op="activate_agent", at="after_round:1",
                        args={"agent": "referee", "position": "after:alice"})
    with pytest.raises(ApplyError, match="phased rounds"):
        activation(mutation, game())
