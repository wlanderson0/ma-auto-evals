"""The board study's briefs (plan ``docs/level2-milestone6.md``, scope items 3 and 4,
step 4): the fixed grid and L2's short brief. Offline, on the mock models."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from experiments.l2.brief import parse_brief
from experiments.l2.experiment import build, run_experiment
from experiments.l2.results import Results
from experiments.scenarios import TEAM
from tests.support.l2_audit import InProcess

BRIEFS = Path(__file__).resolve().parents[2] / "experiments" / "l2" / "briefs"
GRID, L2 = BRIEFS / "board-study-grid.md", BRIEFS / "board-study-l2.md"
RULES = {"plain": "board_workaround", "soft": "board_workaround_soft"}


def grid():
    return parse_brief(GRID).variants


def parts(vid: str) -> tuple[str, str, str]:
    model, rule, opening = vid.split("-")
    return model, rule, opening


def test_the_grid_is_three_models_by_two_rules_by_two_openings():
    brief = parse_brief(GRID)
    ids = [v.id for v in brief.variants]
    assert sorted(ids) == sorted(f"{m}-{r}-{o}" for m in ("m1", "m2", "m3")
                                 for r in RULES for o in ("B1", "B0"))
    assert brief.measure and "Holm" in brief.measure and "uses" in brief.measure
    assert brief.budget_usd == 10 and brief.base_scenarios == tuple(RULES.values())
    assert all(v.inner.condition == "scripted" for v in brief.variants)


def test_the_openings_are_the_calibrations_byte_for_byte():
    calibration = {v.id: v.inner.opening for v in parse_brief(BRIEFS /
                                                               "bench-board-calibration.md").variants}
    for variant in grid():
        assert variant.inner.opening == calibration[parts(variant.id)[2]]


def test_every_agent_runs_on_its_variants_model_and_m1_is_gpt_4_1_mini():
    models = {}
    for variant in grid():
        _, built, problems = build(variant, mock=False)  # builds only: no model is called
        assert built is not None and not problems.rejected, problems
        agents = {spec.name: spec.model for spec in built.task["setup"].agents}
        assert set(agents) == set(TEAM) and len(set(agents.values())) == 1
        [model] = set(agents.values())
        # Set explicitly on every agent, seat included, and nothing else changed.
        assert sorted((m.op, m.args["agent"], m.args["model"]) for m in variant.mutations) == (
            [("set_model", agent, model) for agent in sorted(TEAM)])
        assert variant.rationale.startswith(model.split("/")[-1].removesuffix("-instruct") + ",")
        models.setdefault(parts(variant.id)[0], set()).add(model)
    assert all(len(found) == 1 for found in models.values())
    assert len(set().union(*models.values())) == 3
    assert models["m1"] == {"openrouter/openai/gpt-4.1-mini"}


def test_variants_differ_pairwise_only_in_what_their_ids_say():
    for a, b in itertools.combinations(grid(), 2):
        (ma, ra, oa), (mb, rb, ob) = parts(a.id), parts(b.id)
        assert (a.base == b.base) == (ra == rb)
        assert a.base == RULES[ra]
        assert (a.mutations == b.mutations) == (ma == mb)
        assert (a.inner.opening == b.inner.opening) == (oa == ob)
        assert (a.inner.condition, a.inner.brief, a.inner.scope, a.inner.limits) == (
            b.inner.condition, b.inner.brief, b.inner.scope, b.inner.limits)


def test_every_variant_builds_and_runs_on_the_mocks(tmp_path):
    runs = run_experiment(GRID, 1, tmp_path / "exp", mock=True, launch=InProcess())
    assert len(runs) == 12
    assert all(not r.problems.rejected and not r.problems.warnings for r in runs), [
        (r.variant.id, r.problems) for r in runs if r.problems.rejected or r.problems.warnings]
    assert all(r.epochs_done == 1 for r in runs)
    results = Results.load(tmp_path / "exp")
    assert not results.problems
    for run in runs:
        row = results.compare(run.variant.id, run.variant.id, "outcome", field="uses")
        assert set(row) == {"bob", "carol", "dave"}
        assert all(r["a"]["n"] == 1 and r["a"]["incomplete"] == 0 for r in row.values())


def test_the_l2_brief_is_short_and_names_only_its_bases():
    brief = parse_brief(L2)
    assert brief.base_scenarios == tuple(RULES.values())
    assert brief.measure is brief.variants is brief.controls is brief.mode is None
    assert brief.allowed_changes is brief.inner is brief.hypotheses is brief.base_logs is None
    assert brief.budget_usd == pytest.approx(brief.run_budget_usd + 2)
    assert "#" not in L2.read_text()  # no comments for L2 to read in its audit's copy
