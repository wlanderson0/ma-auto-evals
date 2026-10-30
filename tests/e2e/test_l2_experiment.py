"""``run_experiment`` and the audit record (L2 Task 9; spec §5, §8; L2-2, L2-12, L2-14).

Offline on the mock models. Most tests run each epoch in-process (``run_epoch``
as the launcher, which the real subprocess also calls); one test goes through
the subprocess entry point itself.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
import yaml
from inspect_ai.log import read_eval_log

from experiments.l2.brief import parse_brief
from experiments.l2.experiment import (
    RUN_CONFIG,
    estimate,
    launch_subprocess,
    one_model,
    resolve,
    run_epoch,
    run_experiment,
    run_models,
    token_estimate,
)
from experiments.l2.experiment import build as build_variant
from experiments.run import AUDITOR

MILESTONE1 = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / "milestone1.md"


def subset(tmp_path: Path, ids: list[str], budget: float = 1.0, broken: bool = False,
           warned: bool = False) -> Path:
    """A brief with some milestone 1 variants (anchors resolved); ``broken`` adds a
    variant whose opening is on a channel Alice cannot write, ``warned`` one where Dave
    cannot be reached."""
    text = MILESTONE1.read_text()
    header = yaml.safe_load(re.match(r"---\n(.*?)\n---\n", text, re.S)[1])
    header = {k: v for k, v in header.items() if not k.startswith("x-")}
    header["variants"] = [v for v in header["variants"] if v["id"] in ids]
    header["budget_usd"] = budget
    if broken:
        bad = json.loads(json.dumps(header["variants"][0]))
        bad.update(id="BAD", rationale="opening on #pod, which Alice cannot write")
        bad["inner"] = {**bad.get("inner", {}),
                        "opening": [["pod", bad["inner"]["opening"][0][1]]]}
        header["variants"].append(bad)
    if warned:
        cut = json.loads(json.dumps(header["variants"][0]))
        cut.update(id="CUT", rationale="Dave off #pod's readers")
        cut["mutations"] = [*cut.get("mutations", []), {
            "axis": "topology", "op": "remove_reader", "args": {"agent": "dave",
                                                                "channel": "pod"}}]
        header["variants"].append(cut)
    path = tmp_path / "brief.md"
    path.write_text("---\n" + yaml.safe_dump(header, sort_keys=False) + "---\nQ?\n")
    return path


class Launcher:
    """Runs each epoch in-process, recording what it was asked to run."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[tuple[list[str], int]] = []
        self.briefs: list[Path] = []
        self.fail_at = fail_at

    def __call__(self, brief, ids, epoch, out, mock, scenarios=None):
        self.calls.append((list(ids), epoch))
        self.briefs.append(Path(brief))
        paths = run_epoch(brief, ids, epoch, out, mock, scenarios)
        if epoch == self.fail_at:
            raise RuntimeError("the epoch's process died")
        return paths


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def fixed(usd: float):
    """A cost function charging ``usd`` per log."""
    return lambda log: {"usd": usd, "samples": [{"usd": usd}]}


def test_a_rejected_variant_is_recorded_and_never_run(tmp_path):
    """L2-2."""
    launch = Launcher()
    runs = run_experiment(subset(tmp_path, ["K1"], broken=True), 1, tmp_path / "out",
                          mock=True, launch=launch)
    assert launch.calls == [(["K1"], 1)]
    recorded = {v["id"]: v for v in lines(tmp_path / "out" / "variants.jsonl")}
    assert recorded["BAD"]["status"] == "rejected"
    assert any("posts on 'pod'" in r for r in recorded["BAD"]["rejections"])
    assert recorded["K1"]["status"] == "accepted"
    by_id = {run.variant.id: run for run in runs}
    assert by_id["BAD"].epochs_done == 0 and by_id["BAD"].logs == ()
    assert by_id["BAD"].problems.rejected
    rows = {row["variant"]["id"]: row for row in lines(tmp_path / "out" / "runs.jsonl")}
    assert rows["BAD"]["rejections"] and rows["BAD"]["logs"] == []


def test_epochs_run_one_call_each_over_every_variant(tmp_path):
    """Epoch-major: two calls, each over both variants; logs carry their L2 epoch."""
    launch = Launcher()
    out = tmp_path / "out"
    runs = run_experiment(subset(tmp_path, ["M7", "K2"]), 2, out, mock=True, launch=launch)
    assert launch.calls == [(["M7", "K2"], 1), (["M7", "K2"], 2)]
    assert launch.briefs == [out / "brief.md"] * 2  # the recorded copy, not the original
    for run in runs:
        assert run.epochs_done == 2 and len(run.logs) == 2
        for epoch, path in enumerate(run.logs, start=1):
            assert not Path(path).is_absolute()  # relative to the audit directory
            log = read_eval_log(str(resolve(path, out)))
            assert log.status == "success"
            assert log.eval.metadata["l2"]["epoch"] == epoch
            assert log.eval.metadata["l2"]["variant_id"] == run.variant.id
            assert f"epoch-{epoch}" in path
            # Every call, the Advocate's and the scorers' included, went to a mock.
            assert all(model.startswith("mockllm/") for model in log.samples[0].model_usage)
    # The Advocate ran on the mock (its live model was replaced), after round 1.
    m7 = read_eval_log(str(resolve([r for r in runs if r.variant.id == "M7"][0].logs[0], out)))
    assert {p["sender"] for p in m7.samples[0].store["CommunicationState:messages"]} >= {
        "erin"}
    provenance = json.loads((out / "decisions.json").read_text())["provenance"]
    assert provenance["models"] == {"all": "mockllm (scripted mocks)"}
    assert provenance["commit"] and provenance["prices"]
    for name in ("brief.md", "variants.jsonl", "runs.jsonl", "decisions.json", "cost.json"):
        assert (out / name).exists(), name
    assert (out / "brief.md").read_text() == (tmp_path / "brief.md").read_text()
    assert json.loads((out / "decisions.json").read_text())["stopped"] is None


def on_one_model(path: Path, vid: str, model: str) -> Path:
    """``path``'s brief with ``vid`` copied as ``vid``-one, every agent set to ``model``."""
    header = yaml.safe_load(re.match(r"---\n(.*?)\n---\n", path.read_text(), re.S)[1])
    [base] = [v for v in header["variants"] if v["id"] == vid]
    one = json.loads(json.dumps(base))
    one.update(id=f"{vid}-one", rationale="every agent on one model of its own")
    one["mutations"] = [*one.get("mutations", []), *(
        {"axis": "models", "op": "set_model", "args": {"agent": agent, "model": model}}
        for agent in ("alice", "bob", "carol", "dave"))]
    header["variants"] = [one]
    path.write_text("---\n" + yaml.safe_dump(header, sort_keys=False) + "---\nQ?\n")
    return path


def test_every_model_call_carries_the_run_config(tmp_path):
    """On the mocks (where agents carry no model name): every agent call, and the
    judge's and probes', carries max_tokens and the reasoning effort (the scorers'
    own, lower max_tokens win for theirs)."""
    out = tmp_path / "out"
    brief = on_one_model(subset(tmp_path, ["K1"]), "K1", "openrouter/openai/gpt-4o-mini")
    [run] = run_experiment(brief, 1, out, mock=True, launch=Launcher())
    decisions = json.loads((out / "decisions.json").read_text())
    assert decisions["run_config"] == {"max_tokens": 1500, "reasoning_effort": "high"}
    assert decisions["without_run_config"] == []
    [sample] = read_eval_log(str(resolve(run.logs[0], out))).samples
    calls = [e for e in sample.events if e.event == "model"]
    by_role = {role: [e.config for e in calls if e.role == role] for role in
               (None, "judge", "probe")}
    assert all(by_role.values()), {r: len(c) for r, c in by_role.items()}
    assert {(c.max_tokens, c.reasoning_effort) for c in by_role[None]} == {(1500, "high")}
    for role in ("judge", "probe"):
        assert {c.reasoning_effort for c in by_role[role]} == {"high"}
        assert all(c.max_tokens is not None and c.max_tokens <= 1500 for c in by_role[role])


def test_agents_with_model_names_get_the_run_config_on_every_call(tmp_path, monkeypatch):
    """The live path, end to end: agents keep the model set_model gave each, and only
    get_model is swapped for the scripted mocks (the agents' model -> the scripted
    target, the judge's -> the scripted judge). Every agent call, the judge's and the
    probes' (asked of the agent's own model) carry the run config."""
    import copy

    from inspect_ai.model import GenerateConfig

    import experiments.l2.experiment as experiment
    from experiments.run import _mock_models
    from tests.support.l2_audit import estimate_of

    mocks, model = _mock_models(), "openrouter/openai/gpt-4o-mini"

    def scripted(name, config=None, memoize=True):
        made = copy.copy(mocks["target"] if name == model else mocks["judge"])
        made.config = made.config.merge(config or GenerateConfig())
        return made

    monkeypatch.setattr(experiment, "get_model", scripted)
    out = tmp_path / "out"
    brief = on_one_model(subset(tmp_path, ["K1"]), "K1", model)
    [run] = run_experiment(brief, 1, out, launch=Launcher(), estimator=estimate_of(0.0))
    assert run.epochs_done == 1 and not run.problems.rejected
    [sample] = read_eval_log(str(resolve(run.logs[0], out))).samples
    calls = [e for e in sample.events if e.event == "model"]
    agents = {e.config.max_tokens for e in calls if e.role is None}
    judged = [e for e in calls if e.role == "judge"]
    assert calls and judged and 1500 in agents
    assert {e.config.reasoning_effort for e in calls} == {"high"}
    assert all(e.config.max_tokens is not None and e.config.max_tokens <= 1500 for e in calls)
    assert {e.model for e in calls} == {"mockllm/model", "mockllm/judge"}
    assert len(agents) > 1  # the probes' own, lower max_tokens, on the agents' model


def test_agents_on_one_named_model_run_on_it_as_the_tasks_configured_default(
        tmp_path, monkeypatch):
    """The live path, built but not run: the agents' names are cleared and their model
    is the task's default, with the run config; agents on several models keep their
    names, and the record lists the variant."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-a-key")  # no call is made
    model = "openrouter/openai/gpt-4o-mini"
    brief = parse_brief(on_one_model(subset(tmp_path, ["K1"]), "K1", model))
    [variant] = brief.variants
    _, built, _ = build_variant(variant, mock=False)
    assert one_model(built, AUDITOR) == model
    ran, default = run_models(built, {"target": AUDITOR})
    assert {spec.model for spec in ran.task["setup"].agents} == {None}
    assert str(default) == model and default.config.max_tokens == RUN_CONFIG.max_tokens
    assert default.config.reasoning_effort == "high"
    mixed = built.task["setup"].agents[0].model_copy(update={"model": AUDITOR})
    setup = built.task["setup"].model_copy(
        update={"agents": [mixed, *built.task["setup"].agents[1:]]})
    several = dataclasses.replace(built, task={**built.task, "setup": setup})
    assert one_model(several, AUDITOR) is None
    kept, default = run_models(several, {"target": AUDITOR})
    assert kept.task["setup"].agents == several.task["setup"].agents
    assert str(default) == AUDITOR


def test_a_budget_stop_leaves_every_variant_at_the_same_epoch(tmp_path):
    """L2-12: the budget is checked between epochs."""
    launch = Launcher()
    out = tmp_path / "out"
    runs = run_experiment(subset(tmp_path, ["M0", "K1"], budget=1.5), 3, out, mock=True,
                          launch=launch, cost_of=fixed(1.0))
    assert [epoch for _, epoch in launch.calls] == [1]
    assert {run.epochs_done for run in runs} == {1}
    decisions = json.loads((out / "decisions.json").read_text())
    assert "before epoch 2" in decisions["stopped"]
    cost = json.loads((out / "cost.json").read_text())
    assert cost["spent_usd"] == 2.0 and cost["by_epoch"] == [{"epoch": 1, "usd": 2.0}]


def test_an_estimate_over_budget_needs_confirmation(tmp_path):
    """L2-14: not launched without confirm=True; the choice is recorded."""
    brief = subset(tmp_path, ["K1"])
    expensive = lambda *args: {"usd": 100.0, "basis": ["test"], "per_variant": {}}  # noqa: E731
    launch = Launcher()
    runs = run_experiment(brief, 1, tmp_path / "a", mock=True, launch=launch,
                          estimator=expensive)
    assert launch.calls == [] and all(run.epochs_done == 0 for run in runs)
    decisions = json.loads((tmp_path / "a" / "decisions.json").read_text())
    assert decisions["launched"] is False and decisions["over_budget"] is True
    assert "exceeds the budget" in decisions["why_not_launched"]
    runs = run_experiment(brief, 1, tmp_path / "b", mock=True, launch=launch,
                          estimator=expensive, confirm=True)
    assert launch.calls == [(["K1"], 1)] and runs[0].epochs_done == 1
    decisions = json.loads((tmp_path / "b" / "decisions.json").read_text())
    assert decisions["launched"] is True and decisions["confirmed"] is True


def test_the_estimate_uses_earlier_runs_when_there_are_some(tmp_path):
    first = tmp_path / "first"
    run_experiment(subset(tmp_path, ["K1"]), 1, first, mock=True, launch=Launcher(),
                   cost_of=fixed(0.25))
    k1, k7 = _brief_variants(tmp_path, ["K1", "K7"])
    live = {v.id: build_variant(v, mock=False)[1] for v in (k1, k7)}
    same = estimate([(k1, live["K1"])], 4, [first], fixed(0.25))
    assert same["basis"] == ["earlier runs"] and same["usd"] == pytest.approx(1.0)
    # K7 has five rounds: K1's history, scaled up by the token estimates' ratio.
    longer = estimate([(k7, live["K7"])], 4, [first], fixed(0.25))
    ratio = token_estimate(live["K7"]) / token_estimate(live["K1"])
    assert longer["basis"] == ["earlier runs, scaled by token counts"] and ratio > 1.5
    assert longer["usd"] == pytest.approx(1.0 * ratio)
    fresh = estimate([(k7, live["K7"])], 4, [], fixed(0.25))
    assert fresh["basis"] == ["token counts"] and fresh["usd"] > 0
    mock = estimate([(k7, live["K7"])], 4, [first], fixed(0.25), True)
    assert mock["usd"] == 0.0 and mock["basis"] == ["mock models (free)"]  # history ignored
    (first / "runs.jsonl").write_text((first / "runs.jsonl").read_text().replace(
        "logs/", "gone/"))
    skipped = estimate([(k1, live["K1"])], 4, [first], fixed(0.25))
    assert skipped["basis"] == ["token counts"] and "missing log" in skipped["history_skipped"][0]


def test_the_token_estimate_counts_the_advocates_turns(tmp_path):
    m6, m7 = _brief_variants(tmp_path, ["M6", "M7"])
    assert token_estimate(build_variant(m7, mock=False)[1]) > token_estimate(
        build_variant(m6, mock=False)[1])


def test_a_warned_variant_runs_with_its_warnings_recorded(tmp_path):
    out = tmp_path / "out"
    runs = run_experiment(subset(tmp_path, ["K1"], warned=True), 1, out, mock=True,
                          launch=Launcher())
    cut = next(run for run in runs if run.variant.id == "CUT")
    assert cut.epochs_done == 1 and not cut.problems.rejected
    rows = {row["variant"]["id"]: row for row in lines(out / "runs.jsonl")}
    assert any("'dave' cannot be reached" in w for w in rows["CUT"]["warnings"])
    assert {v["id"]: v["status"] for v in lines(out / "variants.jsonl")}["CUT"] == "warned"


def test_a_failed_epoch_is_recorded_before_the_error_is_raised(tmp_path):
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="process died"):
        run_experiment(subset(tmp_path, ["M0", "K1"], budget=10.0), 3, out, mock=True,
                       launch=Launcher(fail_at=2), cost_of=fixed(0.5))
    rows = lines(out / "runs.jsonl")
    assert {row["epochs_done"] for row in rows} == {1}  # epoch 1 kept, balanced
    decisions = json.loads((out / "decisions.json").read_text())
    assert decisions["stopped"].startswith("epoch 2 failed: RuntimeError")
    failed = decisions["failed_epoch"]
    assert failed["epoch"] == 2 and len(failed["logs"]) == 2  # its logs, still listed
    assert all(usd == 0.5 for usd in failed["logs"].values())
    cost = json.loads((out / "cost.json").read_text())
    assert cost["spent_usd"] == pytest.approx(2.0)  # epoch 1 (1.0) and the failed epoch 2


def test_an_audit_directory_is_never_reused(tmp_path):
    out = tmp_path / "out"
    run_experiment(subset(tmp_path, ["K1"]), 1, out, mock=True, launch=Launcher())
    with pytest.raises(FileExistsError, match="not empty"):
        run_experiment(subset(tmp_path, ["K1"]), 1, out, mock=True, launch=Launcher())


def _brief_variants(tmp_path, ids):
    folder = tmp_path / "variants"
    folder.mkdir(exist_ok=True)
    return parse_brief(subset(folder, ids)).variants


def test_token_estimates_grow_faster_than_rounds(tmp_path):
    variants = {v.id: v for v in _brief_variants(tmp_path, ["K1", "K6", "K7"])}
    cost = {vid: token_estimate(build_variant(v, mock=False)[1]) for vid, v in variants.items()}
    assert 0 < cost["K6"] < cost["K1"] < cost["K7"]
    assert cost["K7"] / cost["K1"] > 5 / 3  # each turn re-reads a longer conversation


def test_the_subprocess_entry_point_runs_an_epoch(tmp_path):
    out = tmp_path / "out"
    runs = run_experiment(subset(tmp_path, ["K1"]), 1, out, mock=True,
                          launch=launch_subprocess)
    assert runs[0].epochs_done == 1
    assert read_eval_log(str(resolve(runs[0].logs[0], out))).eval.metadata["l2"]["epoch"] == 1
    assert json.loads((out / "epoch-1.json").read_text()) == {"K1": runs[0].logs[0]}


def test_live_epochs_time_out_stuck_calls_and_retry_errored_samples(tmp_path, monkeypatch):
    """A call that never returns must not hang the epoch (it did in the first live run)."""
    import experiments.l2.experiment as experiment

    seen = {}
    real = experiment.inspect_eval

    def spy(tasks, **options):
        seen.update(options)
        return real(tasks, **options)

    monkeypatch.setattr(experiment, "inspect_eval", spy)
    run_experiment(subset(tmp_path, ["K1"]), 1, tmp_path / "out", mock=True, launch=Launcher())
    assert seen["attempt_timeout"] == experiment.ATTEMPT_TIMEOUT
    assert seen["max_retries"] == experiment.MAX_RETRIES
    assert seen["retry_on_error"] == experiment.SAMPLE_RETRIES


def test_the_launch_decision_is_on_disk_before_the_first_epoch(tmp_path):
    out = tmp_path / "out"
    seen = {}

    def launch(brief, ids, epoch, out_dir, mock, scenarios):
        decisions = json.loads((out / "decisions.json").read_text())
        seen.update(launched=decisions["launched"], runs=len(lines(out / "runs.jsonl")),
                    cost=(out / "cost.json").exists())
        return run_epoch(brief, ids, epoch, out_dir, mock, scenarios)

    run_experiment(subset(tmp_path, ["K1"]), 1, out, mock=True, launch=launch)
    assert seen == {"launched": True, "runs": 1, "cost": True}


def test_on_the_mock_models_every_agent_runs_on_the_mock_whatever_its_set_model():
    """A set_model is checked against the price table, then ignored on the mocks."""
    import dataclasses

    from experiments.l2.experiment import build
    from experiments.l2.model import InnerSpec, Mutation, Variant

    spec = Variant(id="X", base="mfa_rumor", inner=InnerSpec(condition="baseline"),
                   rationale="r", mutations=(Mutation("models", "set_model", {
                       "agent": "carol", "model": "openrouter/openai/gpt-4o-mini"}),))
    _, mock, problems = build(spec, mock=True)
    assert not problems.rejected
    assert {a.model for a in mock.task["setup"].agents} == {None}
    _, live, _ = build(spec, mock=False)
    assert {a.name: a.model for a in live.task["setup"].agents}["carol"] == \
        "openrouter/openai/gpt-4o-mini"
    bad = dataclasses.replace(spec, mutations=(Mutation("models", "set_model", {
        "agent": "carol", "model": "openrouter/acme/x"}),))
    assert build(bad, mock=True)[2].rejected
