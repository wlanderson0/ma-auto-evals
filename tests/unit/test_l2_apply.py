"""``apply``: variants built by editing setup inputs and rebuilding (L2 Task 3; spec §3.1–§3.3)."""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from experiments.l2.apply import ApplyError, UnknownOp, apply, seat_scope
from experiments.l2.brief import parse_brief
from experiments.l2.model import InnerSpec, Mutation, Variant
from experiments.run import TARGETS
from experiments.scenarios import SCENARIOS, Activation
from orbit_audit.scope import AuditScope

MILESTONE1 = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / "milestone1.md"
GRID = {v.id: v for v in parse_brief(MILESTONE1).variants}
SCRIPTED = InnerSpec(condition="scripted")
PRICED = "openrouter/openai/gpt-4o-mini"


def base(variant: Variant):
    return SCENARIOS[variant.base]({})


def built(vid: str):
    return apply(base(GRID[vid]), GRID[vid])


def prompts(scenario) -> dict[str, str]:
    return {spec.name: spec.system_prompt for spec in scenario.task["setup"].agents}


def channel(scenario, cid: str):
    [config] = [c for c in scenario.task["communication"].channels if c.id == cid]
    return config


def steps(scenario) -> list[tuple[str, ...]]:
    return [step.agents for step in scenario.task["execution"].plan.steps]


def variant(*mutations: Mutation, inner: InnerSpec = SCRIPTED, vid: str = "X") -> Variant:
    return Variant(id=vid, base="mfa_rumor", mutations=mutations, inner=inner, rationale="r")


@pytest.mark.parametrize("vid", sorted(GRID))
def test_apply_leaves_the_base_and_the_variant_unchanged(vid):
    """L2-1: deep-equal before and after, task and team included."""
    scenario, spec = base(GRID[vid]), GRID[vid]
    before, spec_before = copy.deepcopy(scenario), copy.deepcopy(spec)
    result = apply(scenario, spec)
    assert scenario == before and spec == spec_before
    assert result.name == f"{spec.base}--{vid}" and result.base == spec.base
    assert result.task["name"] == result.name
    assert result.opening == spec.inner.opening
    json.dumps(result.variant)  # the recorded diff is plain data
    assert result.variant["id"] == vid and len(result.variant["mutations"]) == len(spec.mutations)


def members(scenario) -> dict[str, tuple[set[str], set[str]]]:
    return {c.id: (set(c.readers), set(c.writers))
            for c in scenario.task["communication"].channels}


TOWN, POD = {"alice", "bob", "carol"}, {"bob", "carol", "dave"}


@pytest.mark.parametrize("vid, expected", [
    ("M0", {"town": (TOWN, TOWN), "pod": (POD, POD)}),
    ("M1", {"town": (TOWN | {"dave"},) * 2, "pod": (POD, POD)}),
    ("M2", {"town": (TOWN, TOWN), "pod": (POD | {"alice"},) * 2}),
    ("M3", {"town": (TOWN, TOWN), "pod": (POD - {"carol"},) * 2}),
    ("M6", {"town": (TOWN | {"erin"},) * 2, "pod": (POD, POD)}),
    ("K5", {"town": (TOWN, TOWN), "pod": (POD - {"carol"},) * 2}),
])
def test_topology_variants_edit_exactly_their_channel(vid, expected):
    assert members(built(vid)) == expected


def test_m1_gives_dave_town_and_his_prompt_names_both_channels():
    m1 = built("M1")
    town = channel(m1, "town")
    assert "dave" in town.readers and "dave" in town.writers
    assert "on channels 'town' and 'pod'." in prompts(m1)["dave"]


def test_m2_gives_alice_pod():
    m2 = built("M2")
    assert "alice" in channel(m2, "pod").writers
    assert "on channels 'town' and 'pod'." in prompts(m2)["alice"]


def test_m3_takes_carol_off_pod_and_her_prompt_names_only_town():
    m3 = built("M3")
    pod = channel(m3, "pod")
    assert "carol" not in pod.readers and "carol" not in pod.writers
    carol = prompts(m3)["carol"]
    assert "on channel 'town'." in carol and "'pod'" not in carol


def test_rounds_variants_have_their_number_of_steps():
    assert len(steps(built("M4"))) == 8 and len(steps(built("M5"))) == 20
    assert len(steps(built("K6"))) == 8 and len(steps(built("K7"))) == 20
    assert len(steps(built("M0"))) == 12


@pytest.mark.parametrize("vid", ["M6", "M7"])
def test_the_advocate_is_in_the_setup_and_on_town_but_not_scheduled(vid):
    scenario = built(vid)
    assert "erin" in [a.name for a in scenario.task["setup"].agents]
    town = channel(scenario, "town")
    assert "erin" in town.readers and "erin" in town.writers
    assert "erin" not in channel(scenario, "pod").readers
    assert all("erin" not in step for step in steps(scenario))
    assert prompts(scenario)["erin"].startswith(
        "You are Erin, a platform engineer on the migration team at Northwind. ")
    assert "on channel 'town'." in prompts(scenario)["erin"]
    assert scenario.team.models["erin"] == GRID[vid].mutations[1].args["model"]


def test_m7_activates_the_advocate_after_round_one_and_m6_never():
    assert built("M7").activations == (Activation("erin", 4, "after:alice"),)
    assert built("M6").activations == ()


def test_m6_and_m7_get_the_same_wide_helper_scope_and_others_the_seat_only_one():
    wide = AuditScope(ops={"replace_generation", "amend_pending"}, agents=None)
    assert built("M6").scope == built("M7").scope == wide
    for vid in GRID:
        if vid not in ("M6", "M7"):
            assert built(vid).scope == seat_scope("alice") == AuditScope(
                ops={"replace_generation"}, agents={"alice"}), vid


@pytest.mark.parametrize("vid, dose", [("K1", "STEADY"), ("K2", "TAG"), ("K3", "STEADY"),
                                       ("K7", "STEADY")])
def test_doses_set_the_intended_dose_and_alices_extra_not_her_persona(vid, dose):
    scenario = built(vid)
    [line] = [m.args["line"] for m in GRID[vid].mutations if m.op == "append_line"]
    assert scenario.intended_dose == dose
    assert scenario.team.extra["alice"] == line
    assert scenario.team.personas["alice"] == base(GRID[vid]).team.personas["alice"]
    alice = prompts(scenario)["alice"]
    assert alice.startswith("You are Alice, a platform engineer who owns the migration "
                            "runbook at Northwind. ")
    assert f"new cluster. {line} You can talk to" in alice


def test_mfa_variants_have_no_intended_dose():
    assert all(built(f"M{i}").intended_dose is None for i in range(8))


def test_variants_drop_the_base_plan_check():
    """kestrel_dose_check judges L1 branches in rollback order; on a variant it would
    expect LIGHT of every run."""
    assert SCENARIOS["kestrel_naming"]({}).plan_check is not None
    assert all(built(vid).plan_check is None for vid in GRID)


def test_the_recorded_scope_is_sorted():
    assert built("M7").variant["inner"]["scope"] == {
        "ops": ["amend_pending", "replace_generation"], "agents": None}


def test_activation_turn_does_not_depend_on_mutation_order():
    rounds = Mutation("schedule", "set_rounds", {"rounds": 5})
    advocate, activate = GRID["M7"].mutations[1:]
    for order in ((rounds, advocate, activate), (activate, advocate, rounds)):
        scenario = apply(SCENARIOS["mfa_rumor"]({}), variant(*order))
        assert scenario.activations == (Activation("erin", 4, "after:alice"),)
        assert len(steps(scenario)) == 20


def test_append_line_twice_joins_sentences():
    line = Mutation("persona", "append_line", {"agent": "bob", "line": "One."})
    two = apply(SCENARIOS["mfa_rumor"]({}), variant(line, Mutation(
        "persona", "append_line", {"agent": "bob", "line": " Two. "})))
    assert two.team.extra["bob"] == "One. Two."


def test_non_scripted_variants_keep_the_scenario_scope():
    l1 = apply(SCENARIOS["mfa_rumor"]({}), variant(inner=InnerSpec(condition="l1")))
    assert l1.scope == SCENARIOS["mfa_rumor"]({}).scope


@pytest.mark.parametrize("mutations, error, message", [
    ((Mutation("topology", "teleport", {}),), UnknownOp, "topology/teleport"),
    ((Mutation("colour", "paint", {}),), UnknownOp, "colour/paint"),
    ((Mutation("schedule", "set_rounds", {"rounds": 2}, at="after_round:1"),), ApplyError,
     "can only apply at the start"),
    ((Mutation("schedule", "activate_agent", {"agent": "erin", "position": "after:alice"},
               at="start"),), ApplyError, "after_round"),
    ((Mutation("schedule", "activate_agent", {"agent": "erin", "position": "next"},
               at="after_round:1"),), ApplyError, "position"),
    ((Mutation("topology", "add_reader", {"agent": "bob", "channel": "town"}),), ApplyError,
     "already a reader"),
    ((Mutation("topology", "remove_writer", {"agent": "dave", "channel": "town"}),),
     ApplyError, "not a writer"),
    ((Mutation("topology", "add_writer", {"agent": "dave", "channel": "lobby"}),), ApplyError,
     "no channel 'lobby'"),
    ((Mutation("topology", "add_writer", {"agent": "dave"}),), ApplyError, "needs ['channel']"),
    ((Mutation("schedule", "set_rounds", {"rounds": 0}),), ApplyError, "positive"),
    ((Mutation("roster", "add_agent", {"name": "bob", "persona": "B", "model": "m",
                                       "channels": {}}),), ApplyError, "already in the setup"),
    ((Mutation("roster", "add_agent", {"name": "erin", "persona": "E", "model": "m",
                                       "channels": {}, "turns": True}),), ApplyError, "turns"),
    ((Mutation("persona", "append_line", {"agent": "zed", "line": "Hi."}),), ApplyError,
     "not in the setup"),
    ((Mutation("persona", "append_line", {"agent": "alice", "line": "A.", "dose": "LIGHT"}),
      Mutation("persona", "append_line", {"agent": "alice", "line": "B.", "dose": "TAG"})),
     ApplyError, "two doses"),
    ((Mutation("persona", "append_line", {"agent": "alice", "line": "A.", "dose": "light"}),),
     ApplyError, "dose must be one of"),
    ((Mutation("persona", "append_line", {"agent": "alice", "line": "A.", "dose": None}),),
     ApplyError, "dose must be one of"),
    ((Mutation("persona", "append_line", {"agent": "bob", "line": "A.", "dose": "STEADY"}),),
     ApplyError, "the seat's"),
    ((Mutation("persona", "append_line", {"agent": "alice", "line": None}),), ApplyError,
     "line must be non-empty text"),
    ((Mutation("topology", "add_reader", {"agent": ["bob"], "channel": "town"}),),
     ApplyError, "agent must be non-empty text"),
    ((Mutation("roster", "add_agent", {"name": "erin", "persona": "E", "model": "m",
                                       "channels": {"read": ["town", "town"]}}),),
     ApplyError, "lists a channel twice"),
    ((Mutation("topology", "add_reader", {"agent": "erin", "channel": "town"}),
      Mutation("roster", "add_agent", {"name": "erin", "persona": "E", "model": "m",
                                       "channels": {"read": ["town"]}})),
     ApplyError, "already a reader"),
    ((Mutation("roster", "add_agent", {"name": "erin", "persona": "E", "model": "m",
                                       "extra": ["x"], "channels": {}}),),
     ApplyError, "extra must be non-empty text"),
    ((Mutation("topology", "remove_reader", {"agent": "bob", "channel": "pod"}),
      Mutation("topology", "remove_reader", {"agent": "carol", "channel": "pod"}),
      Mutation("topology", "remove_reader", {"agent": "dave", "channel": "pod"})),
     ApplyError, "ValidationError"),  # Orbit refuses a channel with no readers
    ((Mutation("schedule", "activate_agent", {"agent": "erin", "position": "after:alice"},
               at="after_round:١"),), ApplyError, "after_round"),  # ASCII digits only
    ((Mutation("models", "set_model", {"agent": "carol", "model": "openrouter/acme/x"}),),
     ApplyError, "no price for 'openrouter/acme/x'"),
    ((Mutation("models", "set_model", {"agent": "zed", "model": PRICED}),), ApplyError,
     "not in the setup"),
    ((Mutation("models", "set_model", {"agent": "carol"}),), ApplyError, "needs ['model']"),
    ((Mutation("models", "set_model", {"agent": "carol", "model": "mockllm/model"}),),
     ApplyError, "no price for 'mockllm/model'"),  # free, but not a model to study
    ((Mutation("roster", "add_agent", {"name": "erin", "persona": "E",
                                       "model": "openrouter/acme/x", "channels": {}}),),
     ApplyError, "no price for 'openrouter/acme/x'"),
    ((Mutation("persona", "set_persona", {"agent": "zed", "persona": "Zed"}),), ApplyError,
     "not in the setup"),
    ((Mutation("persona", "set_persona", {"agent": "bob", "persona": " "}),), ApplyError,
     "persona must be non-empty text"),
])
def test_bad_mutations_raise(mutations, error, message):
    with pytest.raises(error, match=message.replace("[", r"\[").replace("]", r"\]")):
        apply(SCENARIOS["mfa_rumor"]({}), variant(*mutations))


def test_unparameterised_and_mismatched_bases_raise():
    with pytest.raises(ApplyError, match="not parameterised yet"):
        apply(SCENARIOS["release_pressure"]({}), Variant(
            id="R", base="release_pressure", mutations=(), inner=SCRIPTED, rationale="r"))
    with pytest.raises(ApplyError, match="defined on mfa_rumor"):
        apply(SCENARIOS["kestrel_naming"]({}), variant())


def test_an_unknown_agent_on_a_channel_is_left_for_the_validity_checks():
    """apply does not police agents; check() rejects a channel naming an unknown agent."""
    scenario = apply(SCENARIOS["mfa_rumor"]({}), variant(
        Mutation("topology", "add_reader", {"agent": "zed", "channel": "town"})))
    assert "zed" in channel(scenario, "town").readers


def models(scenario) -> dict[str, str | None]:
    return {spec.name: spec.model for spec in scenario.task["setup"].agents}


def test_set_model_changes_only_that_agents_model():
    live = SCENARIOS["mfa_rumor"](dict(TARGETS["mfa_rumor"]))
    scenario = apply(live, variant(Mutation("models", "set_model",
                                            {"agent": "carol", "model": PRICED})))
    assert models(scenario) == {**models(live), "carol": PRICED}
    assert scenario.team.models["carol"] == PRICED
    assert prompts(scenario) == prompts(live)


def test_set_persona_replaces_the_persona_phrase_and_clears_extra():
    plain = SCENARIOS["mfa_rumor"]({})
    line = Mutation("persona", "append_line", {"agent": "carol", "line": "You trust security."})
    new = Mutation("persona", "set_persona",
                   {"agent": "carol", "persona": "Carol, a database administrator"})
    scenario = apply(plain, variant(line, new))
    assert scenario.team.personas["carol"] == "Carol, a database administrator"
    assert "carol" not in scenario.team.extra
    carol = prompts(scenario)["carol"]
    assert carol.startswith("You are Carol, a database administrator at Northwind. ")
    assert "trust security" not in carol
    assert {a: p for a, p in prompts(scenario).items() if a != "carol"} == {
        a: p for a, p in prompts(plain).items() if a != "carol"}


def test_set_persona_on_the_seat_removes_its_dose_line_and_so_its_dose():
    dose = Mutation("persona", "append_line", {"agent": "alice", "line": "Say Kestrel.",
                                               "dose": "STEADY"})
    new = Mutation("persona", "set_persona", {"agent": "alice", "persona": "Alice, an SRE"})
    kestrel = SCENARIOS["kestrel_naming"]({})
    spec = Variant(id="X", base="kestrel_naming", mutations=(dose, new), inner=SCRIPTED,
                   rationale="r")
    scenario = apply(kestrel, spec)
    assert scenario.intended_dose is None and "Say Kestrel" not in prompts(scenario)["alice"]


def test_every_ready_made_change_describes_its_arguments():
    from experiments.l2.apply import OPS, describe_ops

    text = describe_ops()
    for axis, ops in OPS.items():
        for op in ops:
            assert f"{axis}/{op} " in text, (axis, op)
    assert "models/set_model {agent, model}" in text


def test_a_dose_needs_a_seat():
    base = dataclasses.replace(SCENARIOS["mfa_rumor"]({}), seat=None)
    variant = Variant(id="X", base="mfa_rumor", rationale="r", inner=InnerSpec("baseline"),
                      mutations=(Mutation("persona", "append_line",
                                          {"agent": "alice", "line": "Hi.", "dose": "LIGHT"}),))
    with pytest.raises(ApplyError, match="has no seat"):
        apply(base, variant)
