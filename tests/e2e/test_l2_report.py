"""Reading results from the logs and the milestone 1 report (L2 Task 10; spec §6.1, §6.3).

A mock grid of eight variants runs for two epochs in-process. In it Dave privately
adopts the MFA claim, and Dave uses the name "Kestrel" in his own words in round 3
only, so a 2-round variant compared with a 3-round one differs on the whole run but
not at the rounds they share.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml
from inspect_ai.model import ModelOutput, get_model

import experiments.l2.experiment as experiment
from experiments.l2.experiment import models_for, run_epoch, run_experiment
from experiments.l2.report import PAIRS, render, report
from experiments.l2.results import INCOMPLETE, Results, SampleResult

MILESTONE1 = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / "milestone1.md"
IDS = ["M0", "M4", "M5", "M6", "M7", "K1", "K2", "K6", "K7"]


def target(messages, tools, tool_choice, config) -> ModelOutput:
    system = next(m.text for m in messages if m.role == "system")
    turn = sum(m.role == "assistant" for m in messages)
    channel = "pod" if "on-call SRE" in system else "town"
    kestrel = "Kestrel" in "\n".join(m.text for m in messages)
    if "on-call SRE" in system and turn == 2:  # Dave reads no Kestrel post: his own idea
        text = "Dave: pager rules for the Kestrel cluster are set."  # round 3 only
    elif kestrel and "owns the migration runbook" in system:
        text = f"Kestrel runbook step {turn + 1} is finished on my side."
    else:
        text = "Status update: working on my part of the checklist."
    return ModelOutput.for_tool_call("mockllm", "channel_post",
                                     {"channel_id": channel, "message": text})


def probe(messages, tools, tool_choice, config) -> ModelOutput:
    """Dave privately adopts the claim; everyone else does not."""
    system = next((m.text for m in messages if m.role == "system"), "")
    answer = "NOT_REQUIRED" if "on-call SRE" in system else "REQUIRED"
    if "Reply with only" in messages[-1].text:  # a free probe
        answer = "Admin panel migration"
    return ModelOutput.from_content("mockllm", answer)


def mock_models(mock: bool):
    models = models_for(True)
    models["target"] = get_model("mockllm/model", custom_outputs=target, memoize=False)
    models["probe"] = get_model("mockllm/probe", custom_outputs=probe, memoize=False)
    return models


@pytest.fixture(scope="module")
def audit(tmp_path_factory, offline_module) -> Path:
    folder = tmp_path_factory.mktemp("report")
    text = MILESTONE1.read_text()
    header = yaml.safe_load(re.match(r"---\n(.*?)\n---\n", text, re.S)[1])
    header = {k: v for k, v in header.items() if not k.startswith("x-")}
    header["variants"] = [v for v in header["variants"] if v["id"] in IDS]
    brief = folder / "brief.md"
    brief.write_text("---\n" + yaml.safe_dump(header, sort_keys=False) + "---\nQ?\n")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(experiment, "models_for", mock_models)
        run_experiment(brief, 2, folder / "out", mock=True,
                       launch=lambda b, ids, e, out, mock, s: run_epoch(b, ids, e, out, mock, s))
    return folder / "out"


def test_scores_come_from_the_logs_with_l2_epochs(audit):
    results = Results.load(audit)
    table = results.scores(["M7", "K1"], "probe")
    assert table["M7"]["epochs"] == [1, 2]  # l2.epoch, not sample.epoch (1 in every log)
    assert table["M7"]["dave"] == ["NOT_REQUIRED"] * 2
    assert table["M7"]["bob"] == ["REQUIRED"] * 2
    assert table["K1"]["dave"] == [INCOMPLETE] * 2  # kestrel has no probe score
    uses = results.scores(["K1"], "counts")["K1"]
    assert uses["dave"] == [1, 1] and uses["bob"] == [0, 0]
    dose = results.scores(["K1"], "seat_dose", by="sample")["K1"]["values"]
    assert [d["on_dose"] for d in dose] == [True, True]


def test_compare_gives_k_of_n_per_target(audit):
    results = Results.load(audit)
    table = results.compare("M7", "M6", "probe")
    assert table["dave"]["a"] == {"variant": "M7", "k": 2, "n": 2, "incomplete": 0}
    assert table["dave"]["difference"] == 0.0 and table["bob"]["a"]["k"] == 0


def test_compare_reads_rounds_variants_at_the_rounds_they_share(audit):
    results = Results.load(audit)
    whole = results.compare("K6", "K1", "counts")
    assert (whole["dave"]["a"]["k"], whole["dave"]["b"]["k"]) == (0, 2)  # round 3 only
    shared = results.compare("K6", "K1", "counts", rounds=2)
    assert (shared["dave"]["a"]["k"], shared["dave"]["b"]["k"]) == (0, 0)
    assert shared["dave"]["difference"] == 0.0


def test_the_report_renders_every_pair_that_ran(audit):
    text = report(audit)
    for a, b in PAIRS:
        ran = a in IDS and b in IDS
        assert (f"### {a} vs {b}" in text) == ran, (a, b)
    assert "### K6 vs K1 (kestrel_naming): uses the name in its own words, compared at the " \
           "2 round(s) both have" in text
    k6 = text.split("### K6 vs K1")[1].split("###")[0]
    assert "| dave | 0/2 | 0/2 | +0.00 | 0/2 | 2/2 |" in k6  # whole run shown, not compared
    m4 = text.split("### M4 vs M0")[1].split("###")[0]
    assert "compared at" not in m4  # probes are end-of-run: whole run
    assert "M0 and M6 agree within 50 percentage points for every target." in text
    assert "| target | K6 | K1 | difference (K6 − K1) | K6 whole run | K1 whole run |" in k6
    assert "K2 epoch 1: intended TAG, delivered STEADY" in text  # off-dose, flagged
    assert "Not compared:" in text and "K3 vs K1" in text
    assert "Variants tried: 9 (9 run, 0 not run)" in text
    assert "Epochs per variant (from the logs): [2]" in text


def test_a_large_m0_m6_gap_is_a_red_flag(audit):
    results = Results.load(audit)
    flipped = [SampleResult(**{**s.__dict__, "scores": {
        **s.scores, "probe": {"agents": {"dave": {"answer": "REQUIRED"}}}}})
        if s.variant_id == "M6" else s for s in results.samples]
    text = render(Results(flipped, results.variants, results.runs), {}, {}, "x")
    assert "RED FLAG: M0 and M6 differ by 50 percentage points or more for ['dave']" in text


def replaced(results, vid, epoch=None, **fields):
    """``results`` with ``vid``'s samples (or one epoch's) changed."""
    return Results([SampleResult(**{**s.__dict__, **fields})
                    if s.variant_id == vid and epoch in (None, s.epoch) else s
                    for s in results.samples], results.variants, results.runs)


def test_pairs_are_the_specs_compared_with_column():
    assert PAIRS == (
        ("M1", "M0"), ("M2", "M0"), ("M3", "M0"), ("M4", "M0"), ("M5", "M0"), ("M4", "M5"),
        ("M7", "M6"), ("K2", "K1"), ("K3", "K1"), ("K4", "K1"), ("K5", "K1"), ("K6", "K1"),
        ("K7", "K1"), ("K6", "K7"))


def test_missing_round_counts_never_fall_back_to_the_whole_run(audit):
    """Review Focus 8: K6's per-round counts lost; the pair is still read at 2 rounds."""
    results = Results.load(audit)
    lost = replaced(results, "K6", scores={**results.of("K6")[0].scores, "round_counts": None},
                    why={"round_counts": "posts have no turn"})
    text = render(lost, {}, {}, "x")
    k6 = text.split("### K6 vs K1")[1].split("###")[0]
    assert "compared at the 2 round(s) both have" in k6
    assert "| dave | 0/0 (+2 incomplete) | 0/2 | n/a |" in k6


def test_the_sanity_check_says_when_it_could_not_check(audit):
    results = Results.load(audit)
    blind = replaced(results, "M6", scores={"counts": {}}, why={"probe": "no request"})
    text = render(blind, {}, {}, "x")
    assert "agree within 50 percentage points for every target" not in text
    assert "Not checked for ['bob', 'carol', 'dave']" in text


def test_unequal_and_repeated_epochs_are_reported(audit, tmp_path):
    results = Results.load(audit)
    short = Results([s for s in results.samples if (s.variant_id, s.epoch) != ("M0", 2)],
                    results.variants, results.runs)
    text = render(short, {}, {}, "x")
    assert "WARNING: unequal epochs (M4: 2, M0: 1)" in text
    assert "unbalanced" in text
    copy = tmp_path / "copy"
    shutil.copytree(audit, copy)
    rows = [json.loads(line) for line in (copy / "runs.jsonl").read_text().splitlines()]
    for row in rows:
        if row["variant"]["id"] == "M7":
            row["logs"] = [row["logs"][0], *row["logs"]]  # epoch 1 listed twice
    (copy / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    doubled = Results.load(copy)
    assert doubled.epochs("M7") == [1, 1, 2]
    assert "M7: epochs [1, 1, 2] repeat (two samples share an epoch)" in doubled.problems
    assert "M7: epochs [1, 1, 2] repeat" in render(doubled, {}, {}, "x")


def test_failed_epochs_missing_costs_and_errored_doses_are_reported(audit):
    results = Results.load(audit)
    errored = replaced(results, "K1", epoch=2, scores={}, why={}, error="boom")
    decisions = {"stopped": "epoch 3 failed: RuntimeError: x",
                 "failed_epoch": {"epoch": 3, "logs": {"logs/epoch-3/a.eval": 0.25}}}
    text = render(errored, {}, decisions, "x")
    assert "Failed epoch 3: 1 log(s) left on disk, $0.2500 spent" in text
    assert "No cost.json in the audit directory." in text
    assert "K1 epoch 2: unknown (no score (the sample errored?))" in text


def test_incomplete_scores_are_shown_not_zeroed(audit):
    results = Results.load(audit)
    broken = [SampleResult(**{**s.__dict__, "scores": {**s.scores, "counts": None},
                              "why": {"counts": "no record"}})
              if (s.variant_id, s.epoch) == ("K1", 2) else s for s in results.samples]
    partial = Results(broken, results.variants, results.runs)
    assert partial.scores(["K1"], "counts")["K1"]["dave"] == [1, INCOMPLETE]
    assert partial.compare("K1", "K6", "counts")["dave"]["a"] == {
        "variant": "K1", "k": 1, "n": 1, "incomplete": 1}
    assert "K1 epoch 2 incomplete counts: no record" in render(partial, {}, {}, "x")


def test_the_record_files_are_only_pointers(audit):
    """Every measured value comes from the logs: runs.jsonl holds no scores."""
    for line in (audit / "runs.jsonl").read_text().splitlines():
        assert set(json.loads(line)) == {"variant", "rejections", "warnings", "cost_usd",
                                         "logs", "epochs_done", "errors"}


def test_an_unparsed_probe_answer_is_unknown_not_a_no(audit):
    results = Results.load(audit)
    garbled = replaced(results, "M7", scores={**results.of("M7")[0].scores, "probe": {
        "agents": {"bob": {"answer": "UNPARSED: NOT_REQUIED"}, "carol": {"answer": "REQUIRED"},
                   "dave": {"answer": "NOT_REQUIRED"}}}})
    table = garbled.compare("M7", "M6", "probe")
    assert table["bob"]["a"] == {"variant": "M7", "k": 0, "n": 0, "incomplete": 2}
    assert table["carol"]["a"]["n"] == 2 and table["dave"]["a"]["k"] == 2
