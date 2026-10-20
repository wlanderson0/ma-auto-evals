"""The measurement scorers, through the mock harness (issue #19, stage 1)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from inspect_ai import eval as inspect_eval
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import Score
from orbit.ids import id_scope, sequential_ids

from experiments.run import LIMITS, MAX_TOKENS, _mock_models, make_policy, measure, run_condition
from experiments.scenarios import SCENARIOS
from experiments.scorers import measurement_scorers
from orbit_audit.task import audit_task


def run(tmp_path: Path, name: str, condition: str, **replace):
    """One mock sample with only the measurement scorers attached: (log, sample)."""
    models = _mock_models()
    models["probe"] = models["judge"]
    models.update(replace)
    scenario = SCENARIOS[name]({})
    task = audit_task(
        policy=lambda: make_policy(condition, scenario, models["auditor"]),
        tool_replay=scenario.tool_replay, limits=LIMITS, scope=scenario.scope, epochs=1,
        scorer=measurement_scorers(scenario, condition), **scenario.task)
    with id_scope(sequential_ids()):
        log = inspect_eval(task, model=models["target"], log_dir=str(tmp_path), display="none",
                           max_samples=1, fail_on_error=False, max_tokens=MAX_TOKENS,
                           model_roles={"judge": models["judge"], "probe": models["probe"]})[0]
    return log, log.samples[0]


def test_each_measurement_is_a_score_in_the_log(tmp_path):
    _, sample = run(tmp_path, "mfa_rumor", "l1")
    assert set(sample.scores) == {"counts", "post_judge", "probe"}
    probe = sample.scores["probe"]
    assert set(probe.value) == {"bob", "carol", "dave"}
    assert probe.metadata["agents"]["dave"]["why"] is None
    assert sample.scores["post_judge"].metadata["final"] == probe.metadata["final"]


def test_the_scenario_decides_which_scorers_exist(tmp_path):
    _, release = run(tmp_path / "r", "release_pressure", "baseline")
    assert set(release.scores) == {"counts", "post_judge", "probe", "verdict"}
    _, kestrel = run(tmp_path / "k", "kestrel_naming", "l0")
    assert set(kestrel.scores) == {"counts", "post_judge", "free_probes"}


def test_every_branch_is_counted_at_l1_for_a_mention_scenario(tmp_path):
    _, sample = run(tmp_path, "kestrel_naming", "l1")
    branches = sample.scores["counts"].metadata["branches"]
    assert list(branches) == ["branch-0"]
    assert set(branches["branch-0"]["agents"]) == {"bob", "carol", "dave"}
    assert "plan" in branches["branch-0"]


def test_a_failing_judge_call_makes_the_score_incomplete_but_not_the_sample(tmp_path):
    def broken(messages, tools, tool_choice, config):
        raise RuntimeError("judge is down")

    log, sample = run(tmp_path, "mfa_rumor", "baseline",
                      judge=get_model("mockllm/judge", custom_outputs=broken, memoize=False))
    assert log.status == "success" and sample.error is None
    score = sample.scores["post_judge"]
    agents = score.metadata["agents"]
    assert agents
    for a in agents.values():
        assert a["flags"] is None
        assert a["flagged_posts"] is None
        assert a["outcome"] is None
        assert "RuntimeError" in a["why"]
    assert score.value == {agent: "" for agent in agents}


def test_run_condition_attaches_the_scorers_and_roles(tmp_path):
    models = _mock_models()
    models["probe"] = models["judge"]
    [row] = run_condition(SCENARIOS["mfa_rumor"]({}), "l1", 1, tmp_path, models)
    scores = read_eval_log(row["log"]).samples[0].scores
    assert set(scores) == {"counts", "post_judge", "probe"}


def test_rows_say_so_when_the_judge_could_not_be_reached(tmp_path):
    def broken(messages, tools, tool_choice, config):
        raise RuntimeError("judge is down")

    models = _mock_models()
    models["probe"] = models["judge"]
    models["judge"] = get_model("mockllm/judge", custom_outputs=broken, memoize=False)
    [row] = run_condition(SCENARIOS["mfa_rumor"]({}), "baseline", 1, tmp_path, models)
    assert row["status"] == "success" and row["error"] is None
    assert row["agents"]
    for agent in row["agents"].values():
        assert agent["flagged_posts"] is None
        assert agent["flags"] == []
        assert "RuntimeError" in agent["judge_error"]


def _down(name: str):
    def broken(messages, tools, tool_choice, config):
        raise RuntimeError(f"{name} is down")

    return get_model(f"mockllm/{name}", custom_outputs=broken, memoize=False)


def test_rows_say_so_when_a_probe_could_not_be_made(tmp_path):
    models = _mock_models()
    models["probe"] = _down("probe")
    [row] = run_condition(SCENARIOS["mfa_rumor"]({}), "baseline", 1, tmp_path, models)
    assert row["status"] == "success" and row["error"] is None
    assert row["agents"]
    for agent in row["agents"].values():
        assert agent["probe"] is None
        assert agent["adopted"] is None
        assert "RuntimeError" in agent["probe_error"]


def test_rows_say_so_when_free_probes_could_not_be_made(tmp_path):
    models = _mock_models()
    models["probe"] = _down("probe")
    [row] = run_condition(SCENARIOS["kestrel_naming"]({}), "l1", 1, tmp_path, models)
    assert row["status"] == "success" and row["error"] is None
    assert row["agents"] and row["branch_results"]
    entries = [*row["agents"].values(),
               *(a for b in row["branch_results"] for a in b["agents"].values())]
    for agent in entries:
        assert agent["free_probes"] == {}
        assert "RuntimeError" in agent["free_probes_error"]


def test_rows_say_so_when_the_verdict_could_not_be_made(tmp_path):
    models = _mock_models()
    models["probe"] = models["judge"]
    models["judge"] = _down("judge")

    def hedging(messages, tools, tool_choice, config):  # no APPROVE/REJECT: the judge decides
        return ModelOutput.for_tool_call("mockllm", "channel_post",
                                         {"channel_id": "team", "message": "Not sure yet."})

    models["target"] = get_model("mockllm/model", custom_outputs=hedging, memoize=False)
    [row] = run_condition(SCENARIOS["release_pressure"]({}), "baseline", 1, tmp_path, models)
    assert row["status"] == "success" and row["error"] is None
    assert row["verdict"] is None
    assert "RuntimeError" in row["verdict_error"]


def test_rows_list_the_scores_that_were_incomplete():
    scenario = SCENARIOS["mfa_rumor"]({})
    sample = SimpleNamespace(
        id=1, epoch=1, error=None, output=None, store={},
        scores={"counts": Score(value="incomplete", metadata={"incomplete": "no x"})})
    [row] = measure(scenario, "baseline", SimpleNamespace(samples=[sample]), [])
    assert row["incomplete"] == {"counts": "no x"}
