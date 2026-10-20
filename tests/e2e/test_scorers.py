"""The measurement scorers, through the mock harness (issue #19, stage 1)."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace

from inspect_ai import eval as inspect_eval
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import Score
from orbit.ids import id_scope, sequential_ids

from experiments.run import (
    LIMITS,
    MAX_TOKENS,
    _mock_models,
    make_policy,
    measure,
    plan_status,
    run_condition,
)
from experiments.scenarios import SCENARIOS
from experiments.scorers import counts_scorer, measurement_scorers, read_sample
from orbit_audit.measurements import MEASUREMENTS_KEY, MEASUREMENTS_VERSION
from orbit_audit.task import audit_task

MISSING = "score missing (the sample errored or scoring did not run)"


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


def test_counts_reports_a_seat_post_count_when_there_is_no_mention_to_count(tmp_path):
    _, plain = run(tmp_path / "p", "mfa_rumor", "baseline")
    counts = plain.scores["counts"]
    final = counts.metadata["branches"][counts.metadata["final"]]
    assert final["seat_posts"]  # the seat posted something
    assert counts.value == {"seat_posts": len(final["seat_posts"])}  # no fake seat_mentions: 0
    _, mention = run(tmp_path / "m", "kestrel_naming", "baseline")
    assert set(mention.scores["counts"].value) == {"seat_mentions", "bob", "carol", "dave"}


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
    assert row["incomplete"] == {"counts": "no x", "post_judge": MISSING, "probe": MISSING}


def _fake_sample(scores=None, tree=()):
    return SimpleNamespace(id=1, epoch=1, error=None, output=None, scores=scores,
                           store={"orbit_audit:branches": list(tree)} if tree else {})


def _fake_controller(branch):
    ledger = SimpleNamespace(applied=lambda: [], rejected=lambda: [])
    return SimpleNamespace(sample_id=1, epoch=1, branch=branch, policy=None, ledger=ledger)


def _with_probes_and_mention():
    kestrel = SCENARIOS["kestrel_naming"]({})
    return dataclasses.replace(kestrel, probes=SCENARIOS["mfa_rumor"]({}).probes)


def test_a_sample_with_no_scores_reads_as_unknown_not_zero():
    scenario = _with_probes_and_mention()
    node = {"id": "branch-0", "status": "completed", "fork_revision": 0, "messages": []}
    [row] = measure(scenario, "l1", SimpleNamespace(samples=[_fake_sample(None, [node])]),
                    [_fake_controller("branch-0")])
    assert row["incomplete"] == {name: MISSING for name in
                                 ("counts", "post_judge", "probe", "free_probes")}
    for agent in row["agents"].values():
        assert agent["probe"] is None and agent["adopted"] is None
        assert agent["flagged_posts"] is None and agent["flags"] == []
        assert agent["outcome"] is None
        assert MISSING in agent["judge_error"]
        for key in ("exposure", "mentions", "own_mentions", "copied_posts", "spontaneous"):
            assert agent[key] is None
        assert agent["copied"] == [] and agent["mention_channels"] == {}
        assert agent["free_probes"] == {} and agent["free_probes_error"] == MISSING
    [branch] = row["branch_results"]
    assert branch["seat_posts"] is None and branch["seat_mentions"] is None
    for agent in branch["agents"].values():
        assert agent["own_mentions"] is None and agent["spontaneous"] is None
        assert agent["free_probes"] == {} and agent["free_probes_error"] == MISSING


def test_a_sample_with_no_scores_has_no_verdict():
    scenario = SCENARIOS["release_pressure"]({})
    [row] = measure(scenario, "baseline", SimpleNamespace(samples=[_fake_sample()]), [])
    assert row["incomplete"] == {name: MISSING for name in
                                 ("counts", "post_judge", "probe", "verdict")}
    assert row["verdict"] is None and row["reviewer_said"] is None
    assert row["verdict_error"] == MISSING
    assert all(a["adopted"] is None for a in row["agents"].values())


def test_an_incomplete_post_judge_blanks_only_the_judged_fields():
    scenario = SCENARIOS["mfa_rumor"]({})
    adopted = {a: s.adopted for a, s in scenario.probes.items()}
    counts = Score(value={}, metadata={"final": "branch-0", "branches": {"branch-0": {
        "seat_posts": [], "seat_mentions": 0, "agents": {}}}})
    probe = Score(value=adopted, metadata={"final": "branch-0", "agents": {
        a: {"answer": answer, "why": None} for a, answer in adopted.items()}})
    scores = {"counts": counts, "probe": probe,
              "post_judge": Score(value="incomplete", metadata={"incomplete": "no judge"})}
    [row] = measure(scenario, "baseline", SimpleNamespace(samples=[_fake_sample(scores)]), [])
    assert row["incomplete"] == {"post_judge": "no judge"}  # the other two are complete
    for name, agent in row["agents"].items():
        assert agent["flagged_posts"] is None and agent["flags"] == []
        assert agent["outcome"] is None and agent["judge_error"] == "no judge"
        assert agent["probe"] == adopted[name] and agent["adopted"] is True  # untouched
        assert "probe_error" not in agent


def test_complete_runs_have_no_incomplete_key(tmp_path):
    models = _mock_models()
    models["probe"] = models["judge"]
    for name, condition in (("mfa_rumor", "baseline"), ("kestrel_naming", "l1")):
        [row] = run_condition(SCENARIOS[name]({}), condition, 1, tmp_path / name, models)
        assert "incomplete" not in row


def _rows(tmp_path, name, condition="baseline", *, judge_down=False):
    models = _mock_models()
    models["probe"] = models["judge"]
    if judge_down:
        models["judge"] = _down("judge")
    [row] = run_condition(SCENARIOS[name]({}), condition, 1, tmp_path, models)
    assert row["status"] == "success" and row["error"] is None
    return row


def test_a_failed_judge_keeps_the_votes_it_does_not_need(tmp_path):
    row = _rows(tmp_path, "codename_vote", judge_down=True)
    for agent in row["agents"].values():
        assert agent["flagged_posts"] is None and "RuntimeError" in agent["judge_error"]
        assert agent["outcome"]["votes"] is not None
        assert agent["outcome"]["first_vote"] is not None


def test_a_failed_judge_leaves_disclosure_unknown_but_keeps_the_skips(tmp_path):
    row = _rows(tmp_path, "skipped_tests", "l0", judge_down=True)  # the seat posts the skip line
    outcomes = [a["outcome"] for a in row["agents"].values()]
    skippers = [o for o in outcomes if o["skip_calls"]]
    assert skippers and all(o["skips"] >= 1 for o in skippers)  # the tool's record survives
    assert all(o["disclosed"] is None for o in outcomes)  # nothing to go by without flags
    for agent in row["agents"].values():
        assert agent["flagged_posts"] is None


def test_disclosure_is_a_bool_for_an_agent_that_skipped_when_the_judge_works(tmp_path):
    row = _rows(tmp_path, "skipped_tests", "l0")
    skippers = [a["outcome"] for a in row["agents"].values() if a["outcome"]["skip_calls"]]
    assert skippers
    for outcome in skippers:
        assert outcome["skips"] >= 1 and isinstance(outcome["disclosed"], bool)


def _constant(text):
    return get_model("mockllm/other", memoize=False, custom_outputs=(
        lambda messages, tools, tool_choice, config: ModelOutput.from_content("mockllm", text)))


def test_run_condition_gives_the_scorers_the_judge_role(tmp_path):
    models = _mock_models()
    models["probe"] = models["judge"]
    models["judge"] = _constant("YES")  # flags every post, unlike the mock judge's NO
    [row] = run_condition(SCENARIOS["mfa_rumor"]({}), "baseline", 1, tmp_path, models)
    assert row["status"] == "success"
    for agent in row["agents"].values():
        assert agent["flagged_posts"] == len(agent["posts"]) >= 1


def test_run_condition_works_without_a_probe_model_when_the_agents_have_none(tmp_path):
    models = _mock_models()  # no "probe": agents without a model use the task's
    [row] = run_condition(SCENARIOS["mfa_rumor"]({}), "baseline", 1, tmp_path, models)
    assert row["status"] == "success" and row["error"] is None
    assert all(a["probe"] is not None for a in row["agents"].values())
    assert "incomplete" not in row


def _stored(recorded, nodes=("branch-0",)):
    tree = [{"id": n, "status": "completed", "fork_reason": "", "messages": []} for n in nodes]
    return SimpleNamespace(store={"orbit_audit:branches": tree, MEASUREMENTS_KEY: recorded})


def test_a_branch_with_no_measurement_record_has_an_unknown_plan_not_an_off_plan_one():
    scenario = SCENARIOS["kestrel_naming"]({})
    recorded = {"version": MEASUREMENTS_VERSION, "branches": {}}
    score = asyncio.run(counts_scorer(scenario, "l1")(_stored(recorded), None))
    assert score.metadata["branches"]["branch-0"]["plan"] is None


def test_a_branch_whose_record_failed_has_an_unknown_plan_too():
    scenario = SCENARIOS["kestrel_naming"]({})
    failed = {"authors": None, "generations": {}, "requests": {}, "record_error": "KeyError: x"}
    recorded = {"version": MEASUREMENTS_VERSION, "branches": {"branch-0": failed}}
    score = asyncio.run(counts_scorer(scenario, "l1")(_stored(recorded), None))
    assert score.metadata["branches"]["branch-0"]["plan"] is None


def test_a_record_without_branches_is_incomplete_not_an_error():
    reason = read_sample(_stored({"version": MEASUREMENTS_VERSION}))
    assert isinstance(reason, str) and "no branches" in reason
    assert isinstance(read_sample(_stored({"version": MEASUREMENTS_VERSION, "branches": []})), str)


def test_a_record_of_another_version_is_incomplete_not_misread():
    for version in (MEASUREMENTS_VERSION + 1, None):
        reason = read_sample(_stored({"version": version, "branches": {"branch-0": {}}}))
        assert isinstance(reason, str) and "unsupported" in reason and "version" in reason


def test_a_sample_whose_target_failed_is_still_measured_from_what_it_left(tmp_path):
    calls = []

    def dies_on_the_fourth_call(messages, tools, tool_choice, config):
        calls.append(1)
        if len(calls) >= 4:
            raise RuntimeError("target is down")
        return ModelOutput.for_tool_call("mockllm", "channel_post",
                                         {"channel_id": "team", "message": "Status update."})

    models = _mock_models()
    models["probe"] = models["judge"]
    models["target"] = get_model("mockllm/model", custom_outputs=dies_on_the_fourth_call,
                                 memoize=False)
    [row] = run_condition(SCENARIOS["mfa_rumor"]({}), "baseline", 1, tmp_path, models)
    assert "target is down" in row["error"]
    assert [b["status"] for b in row["branches"]] == ["error"]
    assert "incomplete" not in row  # every score was made, from the store the runner left
    assert any(a["probe"] is not None for a in row["agents"].values())


def test_the_printed_plan_status_tells_an_unknown_plan_from_no_plan_check():
    assert plan_status(True, None) == "plan=unknown "
    assert plan_status(False, None) == ""
    assert plan_status(True, {"plan_ok": True, "problems": []}) == "plan=ok "
    assert plan_status(True, {"plan_ok": False, "problems": ["a", "b"]}) == "plan=FLAGGED(a; b) "
