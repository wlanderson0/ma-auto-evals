"""The milestone 2 audit ledger and hard stop (plan step 1; spec §7; L2-10 to L2-14).

Offline on the mock models, each epoch in-process; costs are fixed per log so the
budget arithmetic is exact.
"""

from __future__ import annotations

import dataclasses
import json
from functools import partial

import pytest
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, ModelUsage, get_model

import experiments.l2.experiment as experiment
from experiments.l2.audit import Audit
from experiments.l2.brief import parse_brief
from experiments.l2.cost import events_cost, log_cost
from experiments.l2.model import Mutation
from tests.support.l2_audit import DAVE_ON_TOWN, InProcess, brief, estimate_of, fixed, variant


def audit(tmp_path, budget=1.0, cost=0.0, **options):
    path = brief(tmp_path, budget)
    options.setdefault("launch", InProcess())
    return Audit.create(path, tmp_path / "audit", mock=True, cost_of=fixed(cost),
                        **options), path


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_launch_reports_remaining_as_budget_minus_the_ledger_total(tmp_path):
    """L2-13; the ledger adds the experiments' cost.json and L2's own calls."""
    a, path = audit(tmp_path, budget=2.0, cost=0.1)
    a.set_l2_spend({"usd": 0.05, "by_model": {"mockllm/l2": {"usd": 0.05}}})
    result = a.launch([variant(path, "M0", "C0"), variant(path, "M0", "V1", *DAVE_ON_TOWN)], 2)
    assert result["status"] == "ran" and result["experiment"] == "exp-1"
    assert [v["epochs_done"] for v in result["variants"]] == [2, 2]
    spent = json.loads((a.out / "experiments" / "exp-1" / "cost.json").read_text())["spent_usd"]
    assert spent == pytest.approx(0.4)
    budget = result["budget"]
    assert budget["spent_usd"] == pytest.approx(0.45)
    assert budget["remaining_usd"] == pytest.approx(2.0 - 0.45)
    assert budget["by_kind"] == pytest.approx({"experiments": 0.4, "l2": 0.05})
    assert budget["reserve_usd"] == pytest.approx(0.2)
    assert budget["available_for_experiments_usd"] == pytest.approx(2.0 - 0.45 - 0.2)
    ledger = json.loads((a.out / "ledger.json").read_text())
    assert ledger["remaining_usd"] == pytest.approx(budget["remaining_usd"])
    assert ledger["by_experiment"] == pytest.approx({"exp-1": 0.4})
    assert "up to one epoch" in ledger["note"]


def test_the_hard_stop_ends_an_experiment_between_epochs_and_refuses_the_next(tmp_path):
    """L2-11 (no new epoch or experiment starts) and L2-12 (variants stay balanced)."""
    launch = InProcess()
    a, path = audit(tmp_path, budget=1.0, cost=0.23, launch=launch)
    result = a.launch([variant(path, "M0", "C0"), variant(path, "M0", "V1", *DAVE_ON_TOWN)], 4)
    # Available at launch: 1.0 less the 0.1 reserve. Epoch 2 reaches 0.92: past 0.9 (but
    # not past 1.0, so the reserve is what stops it).
    assert [epoch for _, epoch in launch.calls] == [1, 2]
    assert {v["epochs_done"] for v in result["variants"]} == {2}
    assert "before epoch 3" in result["stopped"]
    assert result["budget"]["spent_usd"] == pytest.approx(0.92)
    assert result["budget"]["stopped"] and "hard stop" in result["budget"]["stopped"]
    again = a.launch([variant(path, "M0", "V2", *DAVE_ON_TOWN)], 1)
    assert again["status"] == "refused" and "hard stop" in again["reason"]
    assert [exp.name for exp in a.experiments()] == ["exp-1"]
    assert len(launch.calls) == 2


def test_l2s_own_spend_counts_toward_the_stop(tmp_path):
    launch = InProcess()
    a, path = audit(tmp_path, budget=1.0, cost=0.3, launch=launch)
    a.set_l2_spend({"usd": 0.5})  # available: 1.0 - 0.1 - 0.5 = 0.4 < one epoch (0.6)
    result = a.launch([variant(path, "M0", "C0"), variant(path, "M0", "V1", *DAVE_ON_TOWN)], 3)
    assert [epoch for _, epoch in launch.calls] == [1]
    assert "before epoch 2" in result["stopped"]


def test_reaching_the_reserve_alone_stops_every_launch(tmp_path):
    launch = InProcess()
    a, path = audit(tmp_path, budget=1.0, launch=launch)
    a.set_l2_spend({"usd": 0.9})
    assert a.stopped and a.budget()["available_for_experiments_usd"] == 0
    result = a.launch([variant(path, "M0", "C0")], 1)
    assert result["status"] == "refused" and launch.calls == [] and a.experiments() == []
    assert lines(a.out / "launches.jsonl")[-1]["status"] == "refused"


def test_an_estimate_over_what_is_left_needs_confirmation(tmp_path):
    """L2-14: a warning with the estimate and its basis; runs only with confirm=True;
    the choice is recorded."""
    launch = InProcess()
    a, path = audit(tmp_path, budget=1.0, launch=launch, estimator=estimate_of(5.0))
    v = [variant(path, "M0", "C0")]
    result = a.launch(v, 1)
    assert result["status"] == "needs_confirmation"
    assert result["estimate"]["usd"] == 5.0 and result["estimate"]["basis"] == ["a test figure"]
    assert "confirm=True" in result["reason"] and "a test figure" in result["reason"]
    assert launch.calls == [] and a.experiments() == []
    result = a.launch(v, 1, confirm=True)
    assert result["status"] == "ran" and launch.calls == [(["C0"], 1)]
    decisions = json.loads((a.out / "experiments" / "exp-1" / "decisions.json").read_text())
    assert decisions["over_budget"] and decisions["confirmed"] and decisions["launched"]
    assert [(r["status"], r["confirm"]) for r in lines(a.out / "launches.jsonl")] == [
        ("needs_confirmation", False), ("ran", True)]


def test_one_rejected_variant_means_nothing_runs(tmp_path):
    """L2-2 for a launch: the rejection is returned and recorded, nothing is run."""
    launch = InProcess()
    a, path = audit(tmp_path, launch=launch)
    bad = variant(path, "M0", "BAD", Mutation("topology", "add_reader",
                                              {"agent": "zed", "channel": "town"}))
    result = a.launch([variant(path, "M0", "C0"), bad], 1)
    assert result["status"] == "rejected" and launch.calls == [] and a.experiments() == []
    by_id = {v["id"]: v for v in result["variants"]}
    assert by_id["C0"]["status"] == "accepted" and by_id["BAD"]["rejections"]
    assert [vid for vid, _ in lines(a.out / "launches.jsonl")[0]["rejections"]] == ["BAD"]


@pytest.mark.parametrize("condition", ["l0", "l1"])
def test_milestone2_runs_baseline_and_scripted_only(tmp_path, condition):
    a, path = audit(tmp_path)
    result = a.launch([variant(path, "M0", "C0", condition=condition)], 1)
    assert result["status"] == "rejected"
    assert any("milestone 2 runs only" in r for r in result["variants"][0]["rejections"])


def test_a_baseline_copy_of_a_control_runs(tmp_path):
    a, path = audit(tmp_path)
    result = a.launch([variant(path, "M0", "B0", condition="baseline")], 1)
    assert result["status"] == "ran"


def test_variant_ids_are_unique_in_the_audit(tmp_path):
    a, path = audit(tmp_path)
    assert a.launch([variant(path, "M0", "C0")], 1)["status"] == "ran"
    result = a.launch([variant(path, "M0", "c0"), variant(path, "M0", "C1"),
                       variant(path, "M0", "C1")], 1)
    assert result["status"] == "rejected"
    rejections = [v["rejections"] for v in result["variants"]]
    assert any("already ran in exp-1" in r for r in rejections[0])
    assert rejections[1] == [] and any("used twice" in r for r in rejections[2])


def test_allowed_changes_limit_what_is_added_to_the_control(tmp_path):
    """M0's own persona line is the control's, not a change L2 made."""
    path = brief(tmp_path, 1.0, extra="\nallowed_changes: [topology]")
    a = Audit.create(path, tmp_path / "audit", mock=True, launch=InProcess())
    rounds = Mutation("schedule", "set_rounds", {"rounds": 2})
    result = a.launch([variant(path, "M0", "V1", *DAVE_ON_TOWN), variant(path, "M0", "V2",
                                                                         rounds)], 1)
    by_id = {v["id"]: v for v in result["variants"]}
    assert by_id["V1"]["rejections"] == []
    assert any("do not allow ['schedule']" in r for r in by_id["V2"]["rejections"])


def test_a_variant_must_start_from_a_control_and_add_no_setup_text(tmp_path):
    a, path = audit(tmp_path)
    m0 = variant(path, "M0", "X0")
    no_belief = dataclasses.replace(m0, id="X1", mutations=())
    new_opening = dataclasses.replace(m0, id="X2", inner=dataclasses.replace(
        m0.inner, opening=(("town", "Something else."),)))
    persona = variant(path, "M0", "X3", Mutation("persona", "append_line",
                                                 {"agent": "bob", "line": "You agree."}))
    result = a.launch([no_belief, new_opening, persona], 1)
    rejections = {v["id"]: " ".join(v["rejections"]) for v in result["variants"]}
    assert "does not start from a control" in rejections["X1"]
    assert "does not start from a control" in rejections["X2"]
    assert "setup texts come only from the controls" in rejections["X3"]
    assert a.control_of(variant(path, "M6", "A1")).id == "M6"  # the longest match
    assert a.check([variant(path, "M0", "Y")], controls={"Y": "M6"})[0][2].rejected


def test_spend_from_one_launch_shrinks_the_next(tmp_path):
    launch = InProcess()
    a, path = audit(tmp_path, budget=1.0, cost=0.1, launch=launch)
    a.launch([variant(path, "M0", "C0")], 2)  # 0.2
    result = a.launch([variant(path, "M0", "C1"), variant(path, "M0", "V1", *DAVE_ON_TOWN)], 6)
    assert parse_brief(a.out / "experiments" / "exp-2" / "brief.md").budget_usd == \
        pytest.approx(0.7)
    # 0.2 per epoch: 0.8 >= 0.7 after epoch 4.
    assert [e for ids, e in launch.calls if "C1" in ids] == [1, 2, 3, 4]
    assert result["budget"]["spent_usd"] == pytest.approx(1.0) and a.stopped


def test_a_launch_that_fails_before_its_record_is_still_recorded(tmp_path, monkeypatch):
    import experiments.l2.audit as module

    def broken(*args, **kwargs):
        raise RuntimeError("died before writing anything")
    monkeypatch.setattr(module.m1, "run_experiment", broken)
    a, path = audit(tmp_path)
    result = a.launch([variant(path, "M0", "C0")], 1)
    assert result["status"] == "failed" and "died before" in result["reason"]
    assert result["spent_usd"] == 0.0 and result["variants"][0]["epochs_done"] == 0
    assert lines(a.out / "launches.jsonl")[-1]["status"] == "failed"


def test_refused_and_rejected_launches_also_report_what_is_left(tmp_path):
    """L2-13 holds for every launch result, not only the ones that ran."""
    a, path = audit(tmp_path, budget=2.0)
    a.set_l2_spend({"usd": 0.25})
    rejected = a.launch([variant(path, "M0", "C0", condition="l1")], 1)
    refused = a.launch([variant(path, "M0", "C0")], 0)
    for result in (rejected, refused):
        assert result["budget"]["remaining_usd"] == pytest.approx(1.75)
    assert [r["remaining_usd"] for r in lines(a.out / "launches.jsonl")] == \
        pytest.approx([1.75, 1.75])


def test_l2s_spend_is_checked_and_only_grows(tmp_path):
    a, _ = audit(tmp_path)
    a.set_l2_spend({"usd": 0.2})
    for bad in (-1.0, float("nan"), float("inf"), 0.1, True):
        with pytest.raises(ValueError):
            a.set_l2_spend({"usd": bad})
    assert a.budget()["by_kind"]["l2"] == 0.2


def test_float_dust_never_leaves_room_for_one_more_epoch(tmp_path):
    a, _ = audit(tmp_path, budget=0.7)
    a.set_l2_spend({"usd": 0.7 - 0.07 - 1e-12})
    assert a.stopped and a.budget()["available_for_experiments_usd"] == 0.0


def test_experiments_are_numbered_after_the_highest_one(tmp_path):
    a, path = audit(tmp_path)
    (a.out / "experiments" / "exp-3").mkdir(parents=True)
    result = a.launch([variant(path, "M0", "C0")], 1)
    assert result["experiment"] == "exp-4" and result["status"] == "ran"


def test_the_per_launch_brief_holds_exactly_the_launched_variants(tmp_path):
    a, path = audit(tmp_path, budget=1.0)
    launched = [variant(path, "M6", "A1"), variant(path, "M0", "V1", *DAVE_ON_TOWN)]
    a.launch(launched, 1)
    recorded = parse_brief(a.out / "experiments" / "exp-1" / "brief.md")
    assert recorded.variants == tuple(launched)
    assert recorded.budget_usd == pytest.approx(0.9)  # what was available for experiments
    assert recorded.mode == "characterise"


def test_an_analysis_only_audit_never_launches(tmp_path):
    source = tmp_path / "m1"
    source.mkdir()
    (source / "runs.jsonl").write_text("")
    launch = InProcess()
    a, path = audit(tmp_path, analysis=source, launch=launch)
    result = a.launch([variant(path, "M0", "C0")], 1)
    assert result["status"] == "refused" and "analysis-only" in result["reason"]
    assert a.sources() == [source] and launch.calls == []


def test_a_new_audit_needs_an_empty_directory(tmp_path):
    a, path = audit(tmp_path)
    assert (a.out / "brief.md").read_text() == path.read_text()
    with pytest.raises(FileExistsError):
        Audit.create(path, a.out, mock=True)


TEST_PRICES = {"mockllm/model": (2.0, 10.0), "mockllm/judge": (1.0, 4.0),
               "mockllm/auditor": (1.0, 1.0)}


def priced_target(messages, tools, tool_choice, config) -> ModelOutput:
    system = next(m.text for m in messages if m.role == "system")
    channel = "pod" if "on-call SRE" in system else "town"
    output = ModelOutput.for_tool_call("mockllm", "channel_post",
                                       {"channel_id": channel, "message": "On it."})
    output.usage = ModelUsage(input_tokens=100, output_tokens=10, total_tokens=110)
    return output


def priced_judge(messages, tools, tool_choice, config) -> ModelOutput:
    output = ModelOutput.from_content("mockllm", "NO")
    output.usage = ModelUsage(input_tokens=7, output_tokens=1, total_tokens=8)
    return output


def test_the_ledger_counts_every_model_call_in_the_logs(tmp_path, monkeypatch):
    """L2-10: the ledger total is every experiment log's calls (agents, scorers) at the
    same prices, plus L2's own events."""
    def models(mock):
        found = dict(original(True))
        found["target"] = get_model("mockllm/model", custom_outputs=priced_target,
                                    memoize=False)
        found["judge"] = found["probe"] = get_model("mockllm/judge",
                                                    custom_outputs=priced_judge, memoize=False)
        return found

    original = experiment.models_for
    monkeypatch.setattr(experiment, "models_for", models)
    cost_of = partial(log_cost, prices=TEST_PRICES)
    a = Audit.create(brief(tmp_path, 5.0), tmp_path / "audit", mock=True, launch=InProcess(),
                     cost_of=cost_of)
    path = tmp_path / "brief.md"
    a.launch([variant(path, "M0", "C0"), variant(path, "M0", "V1", *DAVE_ON_TOWN)], 2)
    a.launch([variant(path, "M6", "A0")], 1)
    l2_events = read_eval_log(str(next((a.out / "experiments").rglob("*.eval")))).samples[0].events
    a.set_l2_spend(events_cost(l2_events, TEST_PRICES))  # any model events stand in for L2's
    logs = sorted((a.out / "experiments").rglob("*.eval"))
    assert len(logs) == 2 * 2 + 1
    in_logs = sum(cost_of(read_eval_log(str(p)))["usd"] for p in logs)
    assert in_logs > 0
    ledger = json.loads((a.out / "ledger.json").read_text())
    assert ledger["by_kind"]["experiments"] == pytest.approx(in_logs)
    assert ledger["spent_usd"] == pytest.approx(in_logs + events_cost(l2_events,
                                                                      TEST_PRICES)["usd"])
