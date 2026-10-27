"""The fake L2 end to end on the mock models (plan step 4; L2-10 for a whole audit).

The fake is a mock model replaying fixed tool calls through the real agent, tools,
ledger and record; experiments run in real subprocesses, as they will live.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from inspect_ai.log import read_eval_log

from experiments.l2.agent import finalize, run_l2, token_backstop
from experiments.l2.audit import Audit, main
from experiments.l2.audit_report import write_report
from experiments.l2.cost import log_cost
from experiments.l2.fake import ANALYSIS, FULL, PROMPT, fake_model
from experiments.l2.tools import Tools
from tests.support.l2_audit import brief

PRICES = {"mockllm/l2": (1.0, 2.0)}
"""L2's fake calls are priced so its own spend shows; the experiments' mocks are free."""
LAYOUT = ["brief.md", "decisions.jsonl", "trace.jsonl", "ledger.json", "proposed.jsonl",
          "launches.jsonl", "report.md", "experiments/exp-1/brief.md",
          "experiments/exp-1/runs.jsonl", "experiments/exp-1/cost.json"]


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture(scope="module")
def full(tmp_path_factory, offline_module):
    folder = tmp_path_factory.mktemp("fake")
    audit = Audit.create(brief(folder, 1.0), folder / "audit", mock=True,
                         cost_of=lambda log: log_cost(log, PRICES))
    tools = Tools(audit, on_end=write_report)
    log = run_l2(tools, fake_model(FULL), PROMPT, prices=PRICES)
    return tools, log


def test_the_fake_runs_the_loop_to_end_audit_and_leaves_a_complete_record(full):
    tools, log = full
    out = tools.audit.out
    assert log.status == "success" and tools.conclusions.startswith("Fake L2 (scripted)")
    for name in LAYOUT:
        assert (out / name).exists(), name
    assert list((out / "l2").glob("*.eval"))
    assert [r["tool"] for r in lines(out / "trace.jsonl")] == [t for t, _ in FULL]
    assert all(r["ok"] for r in lines(out / "trace.jsonl"))
    runs = {r["variant"]["id"]: r for r in lines(out / "experiments" / "exp-1" / "runs.jsonl")}
    assert {vid: r["epochs_done"] for vid, r in runs.items()} == {"C0": 2, "V1": 2}
    report = (out / "report.md").read_text()
    assert "Status: ended by L2 (end_audit)." in report and "V1 vs C0 on probe" in report


def test_the_ledger_counts_every_call_in_every_log(full):
    """L2-10: the ledger total is the cost of every model call in every log of the audit,
    L2's own included, and the ledger is final."""
    tools, _ = full
    out = tools.audit.out
    logs = sorted(out.rglob("*.eval"))
    assert len(logs) == 2 * 2 + 1  # 2 variants x 2 epochs, and L2's
    total = sum(log_cost(read_eval_log(str(p)), PRICES)["usd"] for p in logs)
    ledger = json.loads((out / "ledger.json").read_text())
    assert ledger["final"] and total > 0
    assert ledger["spent_usd"] == pytest.approx(total)
    calls = len(FULL)  # one model call per scripted tool call
    assert ledger["by_kind"]["l2"] == pytest.approx(calls * (1000 * 1.0 + 100 * 2.0) / 1e6)
    # What the tools saw during the run is what the log says (end_audit was the last call).
    assert lines(out / "trace.jsonl")[-1]["spent_usd"] == pytest.approx(ledger["spent_usd"])


def test_the_analysis_only_fake_reads_an_existing_audit(full, tmp_path):
    source = full[0].audit.experiments()[0]
    # ANALYSIS's calls, on this audit's variants (ANALYSIS names milestone 1's).
    script = [("list_runs", {}), ("compare", {"a": "V1", "b": "C0", "scorer": "probe"}),
              ("find", {"pattern": "(?i)mfa", "agent": "dave"}), ANALYSIS[-1]]
    audit = Audit.create(brief(tmp_path, 0.5), tmp_path / "audit", mock=True,
                         analysis=source)
    tools = Tools(audit, on_end=write_report)
    log = run_l2(tools, fake_model(script), PROMPT, prices=PRICES)
    assert log.status == "success" and tools.conclusions is not None
    offered = {tool.name for tool in next(
        e for e in read_eval_log(log.location).samples[0].events if e.event == "model").tools}
    assert "run_experiment" not in offered and {"find", "end_audit"} <= offered
    trace = lines(audit.out / "trace.jsonl")
    assert [r["tool"] for r in trace] == [t for t, _ in script] and all(r["ok"] for r in trace)
    assert "| C0 | " in trace[0]["result"] and audit.experiments() == []
    assert not (audit.out / "experiments").exists()


def test_running_out_of_budget_stops_l2_and_the_report_says_why(tmp_path):
    """With L2's calls dearer than the budget, the loop stops; the record is complete.
    (A large token limit, so the ledger, not the backstop, is what stops it.)"""
    audit = Audit.create(brief(tmp_path, 0.002), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    run_l2(tools, fake_model(FULL), PROMPT, prices=PRICES,
           token_limit=10**6)  # $0.0012 per call
    trace = lines(audit.out / "trace.jsonl")
    assert trace[0]["ok"]  # spent 0.0012 + 0.0002 reserve < 0.002
    assert not trace[1]["ok"] and "hard stop" in trace[1]["result"]
    assert audit.experiments() == []
    report = (audit.out / "report.md").read_text()
    assert "not ended by L2: L2 spent the whole budget" in report
    assert json.loads((audit.out / "ledger.json").read_text())["final"]


def test_the_token_backstop_is_the_budget_at_the_input_rate(tmp_path):
    assert token_backstop(0.5, "openrouter/openai/gpt-4.1-mini") == int(0.5e6 / 0.4)
    assert token_backstop(0.5, "mockllm/l2") is None
    # Applied by default: $0.001 at $1/M input is 1000 tokens, under one fake call (1100).
    audit = Audit.create(brief(tmp_path, 0.001), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    log = run_l2(tools, fake_model(FULL), PROMPT, prices=PRICES)
    sample = read_eval_log(log.location).samples[0]
    assert sample.limit is not None and sample.limit.type == "token"
    assert "the token limit (1000) stopped L2" in (audit.out / "report.md").read_text()


def test_finalize_writes_a_report_when_l2_never_ended(full, tmp_path):
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    tools.stop_reason = None
    finalize(tools, full[1], PRICES)
    assert "not ended by L2: L2 stopped without calling end_audit" in (
        audit.out / "report.md").read_text()


def test_the_cli_runs_the_fake_on_mocks(tmp_path, capsys):
    out = tmp_path / "cli"
    main(["--brief", str(brief(tmp_path, 1.0)), "--out", str(out), "--mock", "--fake"])
    assert (out / "report.md").exists() and (out / "experiments" / "exp-1").exists()
    printed = capsys.readouterr().out
    assert "report:" in printed and "spent $0.0000 of $1.00" in printed
    with pytest.raises(SystemExit, match="use --mock"):  # no live spend before review
        main(["--brief", str(brief(tmp_path, 1.0)), "--out", str(tmp_path / "y"), "--fake"])
    assert not (tmp_path / "y").exists()


def test_a_refused_end_audit_is_retried_and_the_message_limit_backstops(tmp_path,
                                                                         monkeypatch):
    import experiments.l2.agent as agent
    monkeypatch.setattr(agent, "MESSAGE_LIMIT", 12)
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    script = [("end_audit", {"conclusions": " "})] * 50
    run_l2(tools, fake_model(script), PROMPT, prices=PRICES)
    trace = lines(audit.out / "trace.jsonl")
    assert len(trace) > 1 and not any(r["ok"] for r in trace)
    assert "the message limit (12) stopped L2" in (audit.out / "report.md").read_text()


def test_an_unpriced_l2_model_stops_l2_and_still_leaves_a_record(tmp_path, monkeypatch):
    import experiments.l2.cost as cost
    monkeypatch.setattr(cost, "FREE_PREFIXES", ())  # mockllm/l2 now has no price
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    run_l2(tools, fake_model(FULL), PROMPT, prices={})
    [row] = lines(audit.out / "trace.jsonl")
    assert not row["ok"] and "cannot be read" in row["result"]
    ledger = json.loads((audit.out / "ledger.json").read_text())
    assert ledger["final"] and "could not be priced" in ledger["problems"][0]
    assert "L2's own spend could not be read" in (audit.out / "report.md").read_text()


def test_a_log_that_lost_its_samples_never_lowers_the_ledger(full, tmp_path):
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    audit.set_l2_spend({"usd": 0.0036})
    cancelled = full[1].model_copy(update={"samples": None, "status": "cancelled",
                                           "location": ""})
    cancelled.stats.model_usage = {}
    finalize(tools, cancelled, PRICES)
    ledger = json.loads((audit.out / "ledger.json").read_text())
    assert ledger["by_kind"]["l2"] == pytest.approx(0.0036) and ledger["final"]
    assert "the higher is kept" in ledger["problems"][0]
    assert "with status cancelled" in (audit.out / "report.md").read_text()


def test_the_cli_runs_the_analysis_only_fake_on_the_analysis_brief(full, tmp_path, capsys):
    """ANALYSIS names milestone 1's ids; here they are missing, so its queries are
    refused, but the run and its record still complete."""
    analysis = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / \
        "milestone2-analysis.md"
    out = tmp_path / "a"
    main(["--brief", str(analysis), "--out", str(out), "--fake",
          "--analysis-only", str(full[0].audit.experiments()[0])])
    report = (out / "report.md").read_text()
    assert "Analysis only, on the runs in" in report and "Status: ended by L2" in report
