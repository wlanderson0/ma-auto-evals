"""The measurement scorers, through the mock harness (issue #19, stage 1)."""

from __future__ import annotations

from pathlib import Path

from inspect_ai import eval as inspect_eval
from inspect_ai.model import get_model
from orbit.ids import id_scope, sequential_ids

from experiments.run import LIMITS, MAX_TOKENS, _mock_models, make_policy
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
