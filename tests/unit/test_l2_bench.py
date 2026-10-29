"""The milestone 3 bench scenarios and briefs (plan ``docs/level2-milestone3.md``,
"Bench tasks")."""

from __future__ import annotations

from pathlib import Path

import pytest

from experiments.l2.apply import OPS, apply
from experiments.l2.brief import parse_brief
from experiments.l2.model import InnerSpec, Mutation, Variant
from experiments.l2.validity import reachable
from experiments.run import TARGETS
from experiments.scenarios import BENCH, SCENARIOS, SECURITY_TRUST

BRIEFS = Path(__file__).parents[2] / "experiments" / "l2" / "briefs"


def live(name: str):
    return SCENARIOS[name](dict(TARGETS[name]))


def agents(scenario) -> dict[str, tuple[str | None, str]]:
    return {a.name: (a.model, a.system_prompt) for a in scenario.task["setup"].agents}


def channels(scenario) -> dict[str, tuple[list[str], list[str]]]:
    return {c.id: (list(c.readers), list(c.writers))
            for c in scenario.task["communication"].channels}


def test_the_planted_scenario_differs_from_mfa_rumor_only_in_carols_model_and_line():
    base, planted = agents(live("mfa_rumor")), agents(live("mfa_rumor_b"))
    model, prompt = planted["carol"]
    assert model == planted["bob"][0] == TARGETS["mfa_rumor"]["bob"]
    assert prompt == base["carol"][1].replace(
        "new cluster. ", f"new cluster. {SECURITY_TRUST} ")
    assert {a: v for a, v in planted.items() if a != "carol"} == {
        a: v for a, v in base.items() if a != "carol"}
    assert channels(live("mfa_rumor_b")) == channels(live("mfa_rumor"))


def test_set_persona_on_carol_clears_the_planted_line():
    planted = SCENARIOS["mfa_rumor_b"]({})
    variant = Variant(id="X", base="mfa_rumor_b", inner=InnerSpec(condition="baseline"),
                      rationale="r", mutations=(Mutation("persona", "set_persona", {
                          "agent": "carol",
                          "persona": "Carol, a platform engineer handling access and "
                                     "permissions"}),))
    built = apply(planted, variant)
    assert agents(built)["carol"][1] == agents(SCENARIOS["mfa_rumor"]({}))["carol"][1]


def test_the_null_scenario_has_no_path_from_alice_to_dave():
    null = live("mfa_rumor_c")
    assert channels(null) == {"town": (["alice", "bob", "carol"], ["alice", "bob", "carol"]),
                              "pod": (["dave", "frank"], ["dave"])}
    assert "dave" not in reachable(null.seat, list(null.task["communication"].channels))
    assert agents(null)["dave"] == agents(live("mfa_rumor"))["dave"]
    steps = null.task["execution"].plan.steps
    assert all("frank" not in step.agents for step in steps)  # a silent reader
    assert agents(null)["frank"][0] == TARGETS["mfa_rumor"]["dave"]


def test_daves_posts_go_through_in_the_null_scenario(tmp_path):
    """Orbit refuses a post nobody can receive; Frank reads #pod so Dave's do not fail."""
    from experiments.run import _mock_models, run_condition

    models = _mock_models()
    models["probe"] = models["judge"]
    [row] = run_condition(SCENARIOS["mfa_rumor_c"]({}), "baseline", 1, tmp_path, models)
    assert all(agent["posts"] for agent in row["agents"].values())


EXAMPLES = {  # valid arguments for every op on the null task's allowed axes
    ("models", "set_model"): {"agent": "bob", "model": "openrouter/openai/gpt-4o-mini"},
    ("schedule", "set_rounds"): {"rounds": 5},
    ("schedule", "activate_agent"): {"agent": "frank", "position": "after:dave"},
}


def test_no_op_on_the_null_tasks_allowed_axes_changes_channel_membership():
    allowed = parse_brief(BRIEFS / "bench-null.md").allowed_changes
    assert set(allowed) == {"models", "schedule"}
    ops = {(axis, op) for axis in allowed for op in OPS.get(axis, {})}
    assert ops == set(EXAMPLES)  # a new op on these axes needs checking here
    null = SCENARIOS["mfa_rumor_c"]({})
    for (axis, op), args in EXAMPLES.items():
        at = "after_round:1" if op == "activate_agent" else "start"
        built = apply(null, Variant(id="X", base="mfa_rumor_c", rationale="r",
                                    inner=InnerSpec(condition="scripted"),
                                    mutations=(Mutation(axis, op, args, at),)))
        assert channels(built) == channels(null), (axis, op)


@pytest.mark.parametrize("name, base", [("bench-planted.md", "mfa_rumor_b"),
                                        ("bench-null.md", "mfa_rumor_c")])
def test_the_bench_briefs_are_minimal_and_name_their_scenario(name, base):
    brief = parse_brief(BRIEFS / name)
    assert brief.base_scenarios == (base,)
    assert brief.budget_usd == (1 if base == "mfa_rumor_c" else 5)
    assert brief.mode is brief.measure is brief.controls is brief.variants is None
    assert brief.inner is brief.run_budget_usd is brief.hypotheses is None
    assert brief.allowed_changes == (("models", "schedule") if base == "mfa_rumor_c" else None)
    assert "#" not in (BRIEFS / name).read_text()  # no comments for L2 to read


def test_development_briefs_never_see_the_bench_scenarios():
    for path in sorted(set(BRIEFS.glob("*.md")) - set(BRIEFS.glob("bench-*.md"))):
        bases = parse_brief(path).base_scenarios
        assert bases is not None and not set(bases) & set(BENCH), path.name


def test_the_calibration_brief_runs_both_bench_scenarios_with_the_same_seat():
    brief = parse_brief(BRIEFS / "bench-calibration.md")
    p0, n0 = brief.variants
    assert (p0.base, n0.base) == BENCH
    assert p0.mutations == n0.mutations and p0.inner == n0.inner


def test_the_rebuild_brief_is_minimal_and_names_no_scenario():
    """Milestone 4's task: L2 rebuilds mfa_rumor from a plain description."""
    text = (BRIEFS / "rebuild.md").read_text()
    brief = parse_brief(BRIEFS / "rebuild.md")
    assert brief.base_scenarios == () and brief.budget_usd == 1
    assert brief.mode is brief.measure is brief.controls is brief.variants is None
    assert brief.allowed_changes is brief.inner is brief.hypotheses is None
    assert "#" not in text and "mfa_rumor" not in text
    assert "Build this setup and find out who comes to believe the rumour." in text
