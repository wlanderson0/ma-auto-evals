"""Phased rounds as setup inputs (``team_setup(phased=...)``; spec
``docs/level2-interface.md`` §3.5): the checks on a phased team, its steps, how L2
describes it, and the changes L2 cannot make to it. The game itself runs in
``tests/contract/test_phased_rounds.py``."""

from __future__ import annotations

import dataclasses

import pytest
from inspect_ai.model import GenerateConfig
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


def test_an_agents_own_generate_config_reaches_its_spec():
    team = game(generate_configs={"alice": GenerateConfig(reasoning_effort="low")})
    specs = {spec.name: spec for spec in build_team("game", team)["setup"].agents}
    assert specs["alice"].generate_config == GenerateConfig(reasoning_effort="low")
    assert specs["bob"].generate_config is None


def test_a_reasoning_model_gets_the_larger_reply_cap():
    from experiments.l2.experiment import REASONING_MAX_TOKENS, RUN_CONFIG, run_config
    assert run_config("openrouter/openai/gpt-5-nano").max_tokens == REASONING_MAX_TOKENS
    assert run_config("openrouter/openai/gpt-5-nano").reasoning_effort == "high"
    assert run_config("openrouter/openai/gpt-4o-mini") == RUN_CONFIG


def test_a_teams_tools_take_their_arguments_as_strings_too():
    import anyio
    from inspect_ai.tool import ToolDef, tool

    from experiments.scenarios import lenient

    @tool
    def split() -> object:
        async def execute(items: list[str], accept: bool, count: int = 0) -> str:
            """Split.

            Args:
                items: Items.
                accept: Accept.
                count: Count.
            """
            return f"{items} {accept} {count}"
        return execute

    made = lenient(split())
    schema = ToolDef(made).parameters.model_dump(exclude_none=True)["properties"]
    assert schema["accept"]["anyOf"] == [{"type": "boolean"}, {"type": "string"}]
    assert schema["accept"]["description"] == "Accept."
    result = anyio.run(lambda: made(items='["A", "B"]', accept="True", count="2"))
    assert result == "['A', 'B'] True 2"
    with pytest.raises(Exception, match="'yes' is not of type 'boolean'"):
        anyio.run(lambda: made(items=["A"], accept="yes"))
