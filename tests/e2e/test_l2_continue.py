"""Continuing an audit (``--continue``), and how an audit concludes when its budget runs
short (issue #54): the closing reserve, an end_audit sent before the review, the last turn
after the review, and the reviewed draft kept when L2 never revises it.

The fake L2 and the fake reviewer (``experiments.l2.fake``) run on the mock models,
offline throughout.
"""

from __future__ import annotations

import json

import pytest

import experiments.l2.agent as agent
from experiments.l2.agent import brief_message, run_l2
from experiments.l2.audit import CARRIED, Audit, main
from experiments.l2.audit_report import write_report
from experiments.l2.fake import _CLAIMS, DAVE_ON_TOWN, FULL, PROMPT, REVIEW, fake_model
from experiments.l2.tools import Tools
from tests.support.l2_audit import brief

PRICES = {"mockllm/l2": (1.0, 2.0)}  # $0.0012 per fake call
CALL = 0.0012
DRAFT = {"conclusions": "Draft: nothing is concluded.", "claims": "[]", "writeup": "Draft."}
FINAL = {"conclusions": "Final: nothing is concluded.", "claims": "[]", "writeup": "Final."}


@pytest.fixture(scope="module")
def earlier(tmp_path_factory, offline_module):
    """An audit that ran one experiment (exp-1: C0 and V1), was reviewed and ended."""
    folder = tmp_path_factory.mktemp("earlier")
    audit = Audit.create(brief(folder, 1.0), folder / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    run_l2(tools, fake_model(FULL), PROMPT, prices=PRICES)
    assert audit.ended and [p.name for p in audit.experiments()] == ["exp-1"]
    return audit


def carrying(tmp_path, earlier, budget=1.0):
    return Audit.create(brief(tmp_path, budget), tmp_path / "next", mock=True,
                        carry=earlier.out)


# ------------------------------------------------------------- carrying over
def test_a_continued_audit_carries_the_record_but_not_the_spend(tmp_path, earlier):
    audit = carrying(tmp_path, earlier)
    out = audit.out
    for name in ("trace.jsonl", "launches.jsonl", "decisions.jsonl"):
        assert (out / name).read_text() == (earlier.out / name).read_text(), name
    assert [p.name for p in audit.experiments()] == ["exp-1"]
    assert (out / "experiments/exp-1/runs.jsonl").exists()
    assert (out / "carried-review.json").exists()  # shown to L2, not this audit's review
    assert not (out / "review.json").exists() and not (out / "report.md").exists()
    carried = json.loads((out / CARRIED).read_text())
    earlier_ledger = json.loads((earlier.out / "ledger.json").read_text())
    assert carried["experiments"] == ["exp-1"]
    assert carried["spent_usd"] == pytest.approx(earlier_ledger["spent_usd"])
    assert carried["calls"] == len(Tools(earlier).trace())
    b = audit.budget()
    assert b["spent_usd"] == 0 and b["by_experiment"] == {}  # the earlier audit paid
    assert b["carried_usd"] == pytest.approx(earlier_ledger["spent_usd"])
    assert audit.used_ids().keys() == {"c0", "v1"}  # their ids are taken
    tools = Tools(audit)
    assert tools._seq == carried["calls"]  # new calls are numbered after the earlier ones


def test_l2_is_shown_the_earlier_draft_and_the_review(tmp_path, earlier):
    message = brief_message(Tools(carrying(tmp_path, earlier)))
    assert "# Continuing an earlier audit" in message
    assert "exp-1: a pipeline test: one change and its control" in message
    assert "Dave's adoption is not told apart between V1 and C0." in message  # the draft
    assert "(no_difference: a=V1, b=C0, target=dave)" in message  # with its verdict
    assert REVIEW[-1][1]["answer"] in message  # the critique
    assert "Fake L2: the claim is kept as it was." in message  # the response
    assert "is not counted in your budget" in message


def test_continuing_numbers_new_experiments_and_calls_after_the_earlier_ones(
        tmp_path, earlier, offline_module):
    """L2 runs one more experiment and cites an earlier call: the claim is checked as it
    was in the earlier audit, and only the new run is spent from the new budget."""
    audit = carrying(tmp_path, earlier)
    tools = Tools(audit, on_end=write_report)
    first = tools._seq
    conclude = {"conclusions": "Continued: nothing is concluded.", "claims": _CLAIMS,
                "writeup": "Continued."}
    script = [("run_experiment", {"variants": json.dumps([
                  {"id": "V2", "control": "M0", "mutations": DAVE_ON_TOWN,
                   "rationale": "V1 again, in a new experiment"}]),
                  "epochs": 1, "rationale": "a replicate of the earlier run"}),
              ("request_review", conclude),
              ("end_audit", {**conclude, "response": "Kept."})]
    run_l2(tools, fake_model(script), PROMPT, prices=PRICES)
    assert audit.ended and tools.ended_by_harness is None
    assert [p.name for p in audit.experiments()] == ["exp-1", "exp-2"]
    rows = tools.trace()
    assert min(r["seq"] for r in rows[first:]) == first + 1
    assert {r["tool"] for r in rows[first:] if r.get("by") != "reviewer"} == {
        "run_experiment", "request_review", "end_audit"}
    [claim] = tools.claims  # cites call 4, the earlier audit's comparison
    [earlier_claim] = json.loads((earlier.out / "claims.json").read_text())
    assert claim["evidence"] == [{"call": 4}]
    assert claim["verdict"] == earlier_claim["verdict"]
    b = audit.budget()
    assert list(b["by_experiment"]) == ["exp-2"]
    report = (audit.out / "report.md").read_text()
    assert f"This audit continues {earlier.out.resolve()}" in report
    assert "calls 1 to" in report and "is not counted in this audit's budget" in report


def test_continue_refuses_what_it_cannot_continue(tmp_path, earlier):
    with pytest.raises(FileNotFoundError, match="not an L2 audit"):
        Audit.create(brief(tmp_path, 1.0), tmp_path / "a", carry=tmp_path)
    with pytest.raises(FileExistsError, match="not empty"):  # it needs a new directory
        Audit.create(brief(tmp_path, 1.0), earlier.out, carry=earlier.out)
    with pytest.raises(ValueError, match="analysis-only"):
        Audit.create(brief(tmp_path, 1.0), tmp_path / "c", carry=earlier.out,
                     analysis=earlier.out / "experiments" / "exp-1")


def test_the_cli_continues_with_the_earlier_brief(tmp_path, earlier, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(agent, "run_l2", lambda tools, model, prompt, time_limit: seen.append(
        tools.audit))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    with pytest.raises(SystemExit) as stop:  # the stand-in never ends the audit
        main(["--continue", str(earlier.out), "--out", str(tmp_path / "a"), "--mock",
              "--live", "--no-sandbox"])
    assert stop.value.code == 2
    [audit] = seen
    assert audit.carried["experiments"] == ["exp-1"]
    assert (audit.out / "brief.md").read_text() == (earlier.out / "brief.md").read_text()
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["--continue", str(earlier.out), "--question", "Why?", "--budget", "1",
              "--out", str(tmp_path / "b"), "--mock", "--live"])
    assert "--continue takes --brief" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--out", str(tmp_path / "c"), "--mock", "--live"])
    assert "one of --brief, --question or --continue" in capsys.readouterr().err
    assert not (tmp_path / "b").exists() and not (tmp_path / "c").exists()


# --------------------------------------------------------- concluding (#54)


def test_an_end_audit_before_the_review_sends_the_draft_to_the_review(tmp_path,
                                                                      offline_module):
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    run_l2(tools, fake_model([("end_audit", DRAFT),
                              ("end_audit", {**FINAL, "response": "Kept."})]),
           PROMPT, prices=PRICES)
    assert audit.ended and tools.conclusions == "Final: nothing is concluded."
    assert tools.review["draft"]["conclusions"] == "Draft: nothing is concluded."
    l2_rows = [r for r in tools.trace() if r.get("by") != "reviewer"]
    assert [r["tool"] for r in l2_rows] == ["request_review", "end_audit"]
    assert all(r["ok"] for r in l2_rows)  # nothing refused, no turn lost


def test_after_the_review_l2_revises_in_its_closing_turns(tmp_path, offline_module):
    """The review spends what was left; L2 still revises and ends the audit in a closing
    turn (``agent.CLOSING_OVERRUN``)."""
    audit = Audit.create(brief(tmp_path, 0.003), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    run_l2(tools, fake_model([("request_review", DRAFT),
                              ("end_audit", {**FINAL, "response": "Kept."})]),
           PROMPT, prices=PRICES, token_limit=10**6)
    assert audit.budget()["remaining_usd"] < 0  # the closing turn went over
    assert audit.ended and tools.ended_by_harness is None
    assert tools.conclusions == "Final: nothing is concluded."


