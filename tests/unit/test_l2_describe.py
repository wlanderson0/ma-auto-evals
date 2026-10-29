"""What L2 is shown about a scenario's setup (plan ``docs/level2-milestone3.md``,
"What L2 sees")."""

from __future__ import annotations

import pytest

from experiments.l2 import written
from experiments.l2.describe import describe_scenario
from experiments.run import AUDITOR, TARGETS
from experiments.scenarios import SCENARIOS
from tests.support.written import RUMOUR, save


def test_the_description_shows_agents_models_prompts_channels_schedule_and_scorers():
    text = describe_scenario("mfa_rumor", mock=False)
    scenario = SCENARIOS["mfa_rumor"](dict(TARGETS["mfa_rumor"]))
    for spec in scenario.task["setup"].agents:
        assert f"  - {spec.name} ({spec.model}; channel_post): {spec.system_prompt}" in text
    assert "  - #town: read by alice, bob, carol; written by alice, bob, carol" in text
    assert "  - #pod: read by bob, carol, dave; written by bob, carol, dave" in text
    assert "- Schedule: 3 rounds, each: alice; bob; carol; dave." in text
    assert "dave's final reply is the run's output" in text
    assert "- Seat: alice. Targets (measured): bob, carol, dave." in text
    assert scenario.task["dataset"][0].input in text
    probe = scenario.probes["bob"]
    assert f"probe, asked privately of bob, carol, dave: {probe.question}" in text
    assert "NOT_REQUIRED counts as adopting" in text and scenario.judge in text


def test_mock_audits_show_the_mock_models():
    assert "(the run's default model; channel_post)" in describe_scenario("mfa_rumor", True)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_can_be_described(name):
    text = describe_scenario(name, mock=True)
    assert text.startswith(f"## {name} (") and "- Scorers" in text
    if SCENARIOS[name]({}).team is None:
        assert "Not parameterised yet" in text


def test_scenario_specific_scorers_are_shown():
    kestrel = describe_scenario("kestrel_naming", mock=True)
    assert "counts and round_counts: uses of /\\bkestrel\\b/" in kestrel
    assert "free probe" in kestrel and "seat_dose" in kestrel
    assert "outcome (skip_outcome)" in describe_scenario("skipped_tests", mock=True)



def test_a_live_description_names_the_default_model(tmp_path):
    """Live run 1: L2's scenario set no models, and L2 was not told which model that is."""
    code = RUMOUR.replace("team_setup(MODELS, rounds=2)", "team_setup({}, rounds=2)")
    found = written.load(save(tmp_path, "rumour", code).parent)
    assert f"(the run's default model, {AUDITOR}; channel_post)" in describe_scenario(
        "rumour", False, found)
    assert "(the run's default model; channel_post)" in describe_scenario("rumour", True, found)
