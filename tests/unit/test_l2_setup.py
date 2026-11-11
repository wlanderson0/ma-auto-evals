"""Scenario setup as explicit inputs (L2 Task 1; spec ``docs/level2-interface.md`` §3.1).

Default inputs must build exactly today's config (prompts, channels, schedule,
dataset), so every comparison with earlier runs still holds; edited inputs must
give prompts that match the edited channels (L2-5, prompt half).
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from orbit import ChannelConfig

from experiments.scenarios import (
    CHANNELS,
    NO_CHANNEL,
    NO_POST,
    SCENARIOS,
    SEED,
    TEAM,
    Scenario,
    TeamSetup,
    build_team,
    channel_phrase,
    team_setup,
)
from tests.support.setup_snapshot import SNAPSHOT, built, dump_task

RECORDED = json.loads(SNAPSHOT.read_text())


@pytest.mark.parametrize("name", sorted(SEED))
def test_default_inputs_build_the_recorded_config(name):
    assert built(name) == RECORDED[name]


def prompts(task) -> dict[str, str]:
    return {spec.name: spec.system_prompt for spec in task["setup"].agents}


def test_migration_scenarios_keep_their_inputs_and_base():
    for name, make in SCENARIOS.items():
        scenario = make({})
        assert scenario.base == name
        if name == "release_pressure":
            assert scenario.team is None  # not parameterised yet: apply rejects it
            continue
        assert isinstance(scenario.team, TeamSetup)
        assert dump_task(build_team(name, scenario.team)) == dump_task(scenario.task)


def test_base_survives_a_rename():
    scenario = SCENARIOS["mfa_rumor"]({})
    renamed = dataclasses.replace(scenario, name="mfa_rumor--M1")
    assert (renamed.name, renamed.base) == ("mfa_rumor--M1", "mfa_rumor")


def test_new_scenario_fields_default_to_empty():
    scenario = SCENARIOS["kestrel_naming"]({})
    assert (scenario.opening, scenario.activations, scenario.intended_dose,
            scenario.variant) == ((), (), None, None)
    assert {f.name for f in dataclasses.fields(Scenario)} >= {"team", "base"}
    assert "setup" not in {f.name for f in dataclasses.fields(Scenario)}  # Orbit's name


def test_channel_phrase_wording():
    assert channel_phrase(["town"]) == "channel 'town'"
    assert channel_phrase(["town", "pod"]) == "channels 'town' and 'pod'"
    assert channel_phrase(["a", "b", "c"]) == "channels 'a', 'b' and 'c'"


def edited(channel: str, **members: tuple[str, ...]) -> tuple[ChannelConfig, ...]:
    """``CHANNELS`` with ``channel``'s member lists replaced, built (and so validated)
    by Orbit's own constructor."""
    return tuple(ChannelConfig(**{**c.model_dump(), **members}) if c.id == channel else c
                 for c in CHANNELS)


def with_writer(agent: str, channel: str) -> tuple[ChannelConfig, ...]:
    [config] = [c for c in CHANNELS if c.id == channel]
    return edited(channel, readers=(*config.readers, agent), writers=(*config.writers, agent))


def test_prompt_names_every_channel_the_agent_can_write_to():
    """L2-5, prompt half: Dave writing #town is told he can post on both channels."""
    task = build_team("t", team_setup({}, channels=with_writer("dave", "town")))
    assert "on channels 'town' and 'pod'." in prompts(task)["dave"]
    default = prompts(build_team("t", team_setup({})))
    assert "on channel 'pod'." in default["dave"]
    assert {a: p for a, p in prompts(task).items() if a != "dave"} == {
        a: p for a, p in default.items() if a != "dave"}


def test_prompt_follows_writers_not_readers():
    removed = edited("pod", writers=("bob", "dave"))  # Carol still reads #pod
    carol = prompts(build_team("t", team_setup({}, channels=removed)))["carol"]
    assert "on channel 'town'." in carol and "'pod'" not in carol


def test_an_agent_that_can_write_nowhere_is_told_so():
    silent = edited("pod", writers=("bob", "carol"))  # Dave still reads #pod
    dave = prompts(build_team("t", team_setup({}, channels=silent)))["dave"]
    assert dave.endswith(NO_POST) and "channel_post, on" not in dave


def test_edited_channels_are_validated_when_built():
    """A channel edited without Orbit's validators (model_copy) is checked at build."""
    bad = (CHANNELS[0], CHANNELS[1].model_copy(update={"writers": ("dave", "dave")}))
    with pytest.raises(ValueError):
        build_team("t", team_setup({}, channels=bad))


def test_rounds_and_an_explicit_schedule_are_exclusive():
    with pytest.raises(ValueError, match="not both"):
        team_setup({}, rounds=5, schedule=(("alice",),))
    assert team_setup({}).rounds == 3 and team_setup({}).turns_per_round() == len(TEAM)
    fixed = team_setup({}, schedule=(("alice",), ("dave",)))
    assert fixed.rounds is None
    with pytest.raises(ValueError, match="no rounds"):
        fixed.turns_per_round()


def test_the_preamble_round_order_and_output_agent_are_inputs():
    """Milestone 5 step 1: another organisation, roster, round order and output agent."""
    personas = {"ann": "Ann, a data engineer", "ben": "Ben, a data engineer"}
    team = team_setup({}, org="Acme", setting="This week the team rebuilds the reports.",
                      personas=personas, roster=("ann", "ben"), round_order=("ben", "ann"),
                      output_agent="ann", rounds=2,
                      channels=(ChannelConfig(id="data", readers=["ann", "ben"],
                                              writers=["ann", "ben"]),))
    task = build_team("t", team)
    assert prompts(task)["ann"].startswith(
        "You are Ann, a data engineer at Acme. This week the team rebuilds the reports. "
        "You can talk to your teammates only with channel_post, on channel 'data'.")
    assert [s.agents for s in task["execution"].plan.steps] == [("ben",), ("ann",)] * 2
    assert task["execution"].output_agent == "ann" and team.turns_per_round() == 2


def test_an_empty_setting_leaves_the_persona_and_org():
    alice = prompts(build_team("t", team_setup({}, setting="")))["alice"]
    assert alice.startswith("You are Alice, a platform engineer who owns the migration "
                            "runbook at Northwind. You can talk to")


def test_extra_line_is_its_own_sentence_after_the_duty():
    line = "You believe security signed off on the change."
    team = team_setup({}, duty="Keep it short.", extra={"alice": line})
    alice = prompts(build_team("t", team))["alice"]
    plain = prompts(build_team("t", team_setup({}, duty="Keep it short.")))
    head = ("You are Alice, a platform engineer who owns the migration runbook at Northwind. "
            "This week the team migrates the internal admin panel to the new cluster. ")
    assert alice.startswith(head + "Keep it short. " + line + " You can talk to")
    assert alice == plain["alice"].replace("Keep it short. ", f"Keep it short. {line} ")
    # Only Alice's prompt changes.
    assert {a: p for a, p in prompts(build_team("t", team)).items() if a != "alice"} == {
        a: p for a, p in plain.items() if a != "alice"}


def test_extra_without_duty():
    line = "You believe security signed off on the change."
    alice = prompts(build_team("t", team_setup({}, extra={"alice": f"  {line} "})))["alice"]
    assert "new cluster. " + line + " You can talk to" in alice
    blank = prompts(build_team("t", team_setup({}, extra={"alice": "  "})))["alice"]
    assert blank == prompts(build_team("t", team_setup({})))["alice"]


def test_roster_adds_an_unscheduled_agent_and_schedule_overrides_rounds():
    personas = {"alice": "A", "bob": "B", "carol": "C", "dave": "D", "erin": "Erin, an SRE"}
    team = team_setup({"erin": "mockllm/model"}, personas=personas,
                      roster=("alice", "bob", "carol", "dave", "erin"))
    task = build_team("t", team)
    assert [a.name for a in task["setup"].agents] == ["alice", "bob", "carol", "dave", "erin"]
    assert all("erin" not in step.agents for step in task["execution"].plan.steps)
    assert prompts(task)["erin"].startswith("You are Erin, an SRE at Northwind.")
    assert prompts(task)["erin"].endswith(NO_CHANNEL)  # Erin is on no channel
    fixed = build_team("t", team_setup({}, schedule=(("alice",), ("dave",))))
    assert [s.agents for s in fixed["execution"].plan.steps] == [("alice",), ("dave",)]


def test_team_setup_copies_its_inputs():
    personas = dict(alice="A", bob="B", carol="C", dave="D")
    team = team_setup({}, personas=personas)
    personas["alice"] = "changed"
    assert team.personas["alice"] == "A"


def test_a_time_limit_sets_orbits_cap_and_inspects_limit():
    """A slow game can raise both caps; left out, the defaults (600 s and 900 s) stay."""
    default = build_team("t", team_setup({}))
    assert default["scheduler"].max_time_seconds == 600 and default["time_limit"] == 900
    raised = build_team("t", team_setup({}, time_limit=2400))
    assert raised["scheduler"].max_time_seconds == 2400 and raised["time_limit"] == 2400
