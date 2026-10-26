"""Building and running L2 variant tasks (L2 Task 6; spec §5).

Several variants run as one ``inspect_eval`` call (one epoch), each its own task
with its own log; each log records its variant, condition and epoch in its own
task metadata.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from inspect_ai import eval as inspect_eval
from orbit.ids import id_scope, sequential_ids

from experiments.l2.apply import apply
from experiments.l2.brief import parse_brief
from experiments.run import MAX_TOKENS, _mock_models, build_task, l2_metadata, run_condition
from experiments.scenarios import SCENARIOS
from experiments.scorers import expected_scores, measurement_scorers
from orbit_audit.branches import AuditLimits

MILESTONE1 = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / "milestone1.md"
GRID = {v.id: v for v in parse_brief(MILESTONE1).variants}
MESSAGES = "CommunicationState:messages"


def models():
    mock = _mock_models()
    mock["probe"] = mock["judge"]
    return mock


def built(vid: str):
    return apply(SCENARIOS[GRID[vid].base]({}), GRID[vid])


def test_variants_run_as_one_eval_with_a_log_and_metadata_each(tmp_path):
    mock = models()
    scenarios = [built("M3"), built("K2")]
    controllers: dict[str, list] = {s.name: [] for s in scenarios}
    tasks = [build_task(s, "scripted", mock, metadata=l2_metadata(s, "scripted", epoch=2),
                        on_controller=controllers[s.name].append) for s in scenarios]
    with id_scope(sequential_ids()):
        logs = inspect_eval(tasks, model=mock["target"], log_dir=str(tmp_path),
                            display="none", max_tasks=2, epochs=1, fail_on_error=False,
                            score_on_error=True, max_tokens=MAX_TOKENS,
                            model_roles={"judge": mock["judge"], "probe": mock["probe"]})
    assert len(logs) == 2
    by_task = {log.eval.task: log for log in logs}
    assert set(by_task) == {"mfa_rumor--M3", "kestrel_naming--K2"}
    assert len({log.location for log in logs}) == 2  # one log file per variant
    for scenario in scenarios:
        log = by_task[scenario.name]
        assert log.status == "success"
        meta = log.eval.metadata["l2"]
        assert meta == {
            "variant_id": scenario.variant["id"], "base": scenario.base,
            "mutations": scenario.variant["mutations"], "inner": scenario.variant["inner"],
            "condition": "scripted", "rationale": scenario.variant["rationale"], "epoch": 2}
        [sample] = log.samples
        assert sample.epoch == 1  # each log holds one epoch: L2's epoch is in the metadata
        assert set(sample.scores) == set(expected_scores(scenario))
        assert all("incomplete" not in (s.metadata or {}) for s in sample.scores.values())
        assert len(controllers[scenario.name]) == 1
    # Each variant's own setup ran: Carol is off #pod in M3; K2's opening is tagged.
    m3 = by_task["mfa_rumor--M3"].samples[0].store[MESSAGES]
    assert not [p for p in m3 if p["sender"] == "carol" and p["channel_id"] == "pod"]
    assert "carol" not in {r for p in m3 if p["channel_id"] == "pod" for r in p["recipients"]}
    k2 = [p for p in by_task["kestrel_naming--K2"].samples[0].store[MESSAGES]
          if p["sender"] == "alice"]
    assert k2[0]["content"] == GRID["K2"].inner.opening[0][1]
    assert k2[0]["content"].startswith("[Kestrel] ")


def test_run_condition_passes_its_limits_to_the_run(tmp_path):
    """No interventions allowed: the helper's opening is refused, so the sample errors."""
    [row] = run_condition(built("K1"), "scripted", 1, tmp_path, models(),
                          limits=AuditLimits(max_branches=1, max_interventions=0))
    assert row["error"] is not None and "intervention budget exceeded" in row["error"]
    [ok] = run_condition(built("K1"), "scripted", 1, tmp_path / "default", models())
    assert ok["error"] is None and ok["base"] == "kestrel_naming"
    assert ok["scenario"] == "kestrel_naming--K1"


def test_l2_metadata_is_only_for_variants_and_matches_their_condition():
    assert l2_metadata(SCENARIOS["mfa_rumor"]({}), "baseline", epoch=1) is None
    with pytest.raises(ValueError, match="is a scripted variant, not baseline"):
        l2_metadata(built("M3"), "baseline", epoch=1)


def test_l1_and_l0_still_need_an_auditor_model(tmp_path):
    mock = models()
    del mock["auditor"]
    [row] = run_condition(SCENARIOS["mfa_rumor"]({}), "l0", 1, tmp_path, mock)
    assert row["error"] is not None and "auditor" in row["error"]
    [ok] = run_condition(built("M0"), "scripted", 1, tmp_path / "s", mock)  # needs none
    assert ok["error"] is None


def test_scripted_gets_the_baseline_scorers_and_unknown_conditions_fail():
    """Score names on a real run are checked above; here, the condition gate."""
    scenario = built("K1")
    assert len(measurement_scorers(scenario, "scripted")) == len(expected_scores(scenario))
    with pytest.raises(ValueError, match="unknown condition"):
        measurement_scorers(scenario, "l2")


def test_release_pressure_is_recognised_by_base():
    """Lookups by base: a renamed release_pressure still gets its verdict scorer."""
    renamed = dataclasses.replace(SCENARIOS["release_pressure"]({}), name="release_pressure--X")
    assert "verdict" in expected_scores(renamed)
