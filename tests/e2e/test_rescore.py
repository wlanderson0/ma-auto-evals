"""Rescoring a saved log, and scores that cannot be computed (issue #19, stage 1)."""

from __future__ import annotations

import asyncio

from inspect_ai import score_async
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, get_model

from experiments.run import _mock_models, run_condition
from experiments.scenarios import SCENARIOS
from experiments.scorers import measurement_scorers
from orbit_audit.measurements import MEASUREMENTS_KEY
from tests.support.golden import rollback_auditor


def saved_run(tmp_path):
    models = _mock_models()
    models["probe"] = models["judge"]
    scenario = SCENARIOS["release_pressure"]({})
    [row] = run_condition(scenario, "baseline", 1, tmp_path, models)
    return scenario, read_eval_log(row["log"])


def rescore(scenario, log, **roles):
    return asyncio.run(score_async(log, measurement_scorers(scenario, "baseline"),
                                   model_roles=roles, action="overwrite"))


def constant(text):
    return get_model("mockllm/other", memoize=False, custom_outputs=(
        lambda messages, tools, tool_choice, config: ModelOutput.from_content("mockllm", text)))


def test_a_different_judge_rescores_a_saved_log_without_rerunning_the_agents(tmp_path):
    scenario, log = saved_run(tmp_path)
    before = log.samples[0]
    assert before.scores["verdict"].metadata["verdict"] == "REJECT"
    after = rescore(scenario, log, judge=constant("YES"), probe=constant("APPROVE"))
    sample = after.samples[0]
    assert sample.scores["post_judge"].metadata["agents"]["coder"]["flagged_posts"] >= 1
    assert sample.scores["probe"].metadata["agents"]["reviewer"]["answer"] == "APPROVE"
    assert sample.store == before.store  # the agents' run is untouched
    assert sample.messages == before.messages


def test_a_missing_measurements_record_gives_incomplete_scores_not_zeros(tmp_path):
    scenario, log = saved_run(tmp_path)
    del log.samples[0].store[MEASUREMENTS_KEY]
    sample = rescore(scenario, log, judge=constant("YES"), probe=constant("APPROVE")).samples[0]
    assert set(sample.scores) == {"counts", "post_judge", "probe", "verdict"}
    for score in sample.scores.values():
        assert score.value == "incomplete" and MEASUREMENTS_KEY in score.metadata["incomplete"]


def test_a_forked_tree_counts_every_branch_but_judges_only_the_final_one(tmp_path):
    models = _mock_models()
    models["auditor"] = get_model("mockllm/auditor", custom_outputs=rollback_auditor,
                                  memoize=False)
    models["probe"] = models["judge"]
    scenario = SCENARIOS["kestrel_naming"]({})
    [row] = run_condition(scenario, "l1", 1, tmp_path, models)
    assert [b["status"] for b in row["branches"]] == ["rolled_back", "completed"]
    scores = read_eval_log(row["log"]).samples[0].scores
    for name in ("counts", "free_probes"):  # mention counts cover the whole tree
        meta = scores[name].metadata
        assert {"branch-0", "branch-1"} <= set(meta["branches"])
        assert meta["final"] == "branch-1"
    post_judge = scores["post_judge"].metadata  # judged text is the final branch only
    assert post_judge["final"] == "branch-1"
    assert "branches" not in post_judge and post_judge["agents"]
