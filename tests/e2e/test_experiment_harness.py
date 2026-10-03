"""The live experiment harness end to end, offline with scripted models.

Guards ``experiments/run.py`` so a live run does not fail on plumbing: each
scenario runs under the LLM auditor (L1) and the auditor seat (L0), and the
measurements (probes, judged posts, interventions, branches) are produced.
"""

from __future__ import annotations

import pytest

from experiments.run import _mock_models, run_condition
from experiments.scenarios import SCENARIOS


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("condition", ["l1", "l0"])
def test_harness_runs_and_measures_offline(tmp_path, name, condition):
    models = _mock_models()
    models["probe"] = models["judge"]
    scenario = SCENARIOS[name]({})
    [row] = run_condition(scenario, condition, 1, tmp_path, models)
    assert row["status"] == "success" and row["error"] is None
    assert [b["status"] for b in row["branches"]] == ["completed"]
    assert set(row["agents"]) == set(scenario.targets)
    assert all(agent["posts"] for agent in row["agents"].values())
    if condition == "l0":
        # Three seat turns; under the completion quantum each is a post, then a finish.
        assert row["interventions"]["applied"] == {"replace_generation": 6}
        assert row["interventions"]["off_seat"] == []
        assert len(row["seat_posts"]) == 3
    else:
        assert row["auditor"]["kind"] == "LLMAuditor" and row["auditor"]["wakes"] > 0
