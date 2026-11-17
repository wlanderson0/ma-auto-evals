"""An audit is always reviewed and ended (spec §7, "Closing turns"; L2-25): the reserve is sized
from what L2's calls cost, the reviewer always gets a few calls, L2 gets a few closing
turns past the budget, and if it still has not ended the audit the harness ends it with
the draft it sent for review. Offline; each fake call costs $0.0012."""

from __future__ import annotations

import json

import pytest

from experiments.l2.agent import CLOSING_OVERRUN, REVIEW_TURNS, run_l2
from experiments.l2.audit import CLOSING_TURNS, RESERVE, Audit
from experiments.l2.audit_report import write_report
from experiments.l2.fake import PROMPT, REVIEW, fake_model
from experiments.l2.tools import Tools
from tests.support.l2_audit import brief

PRICES = {"mockllm/l2": (1.0, 2.0)}  # $0.0012 per fake call
CALL = 0.0012
DRAFT = {"conclusions": "Draft: nothing is concluded.", "claims": "[]", "writeup": "Draft."}
FINAL = {"conclusions": "Final: nothing is concluded.", "claims": "[]", "writeup": "Final.",
         "response": "Kept."}
SPEND = [("list_runs", {})] * 3  # $0.0036: past a $0.003 budget, every call refused


def audit_of(tmp_path, budget: float) -> Tools:
    return Tools(Audit.create(brief(tmp_path, budget), tmp_path / "audit", mock=True),
                 on_end=write_report)


def test_the_reserve_pays_for_l2s_closing_calls_at_their_price(tmp_path):
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    assert audit.budget()["reserve_usd"] == pytest.approx(RESERVE * 1.0)  # no calls yet
    for spent in (0.01, 0.06, 0.08):  # calls of $0.01, $0.05 and $0.02
        audit.set_l2_spend({"usd": spent})
    b = audit.budget()
    assert b["l2_turn_usd"] == pytest.approx(0.05)  # the dearest of the latest three
    assert b["reserve_usd"] == pytest.approx(CLOSING_TURNS * 0.05)
    for spent in (0.09, 0.10, 0.11):  # three cheap calls push the dear one out
        audit.set_l2_spend({"usd": spent})
    assert audit.budget()["reserve_usd"] == pytest.approx(RESERVE * 1.0)


def test_closing_turns_let_l2_get_its_review_and_end_past_the_budget(tmp_path):
    """The budget is spent before L2 asks for a review: the reviewer still makes its
    calls (its share of a negative remainder would be none), and L2 ends the audit."""
    tools = audit_of(tmp_path, 0.003)
    script = [*SPEND, ("request_review", DRAFT), ("end_audit", FINAL)]
    run_l2(tools, fake_model(script), PROMPT, prices=PRICES, token_limit=10**6)
    trace = [row for row in tools.trace() if row.get("by") != "reviewer"]
    assert [row["tool"] for row in trace] == [t for t, _ in script]
    assert all(not row["ok"] for row in trace[:3]) and all(row["ok"] for row in trace[3:])
    review = tools.review
    assert review["stopped"] is None and review["critique"] == REVIEW[-1][1]["answer"]
    assert review["cost_usd"] == pytest.approx(REVIEW_TURNS * CALL)
    assert tools.conclusions == FINAL["conclusions"] and tools.ended_by_harness is None
    assert review["response"] == "Kept."
    ledger = json.loads((tools.audit.out / "ledger.json").read_text())
    assert ledger["spent_usd"] == pytest.approx((len(script) + len(REVIEW)) * CALL)
    report = (tools.audit.out / "report.md").read_text()
    assert "ended by L2 (end_audit)" in report and "past the budget" in report


def test_the_harness_ends_the_audit_with_the_reviewed_draft(tmp_path):
    """L2 gets its review but never ends the audit: after its closing turns the harness
    ends it with the draft, and the report and write-up say so."""
    tools = audit_of(tmp_path, 0.003)
    script = [*SPEND, ("request_review", DRAFT), *[("list_runs", {})] * 10]
    run_l2(tools, fake_model(script), PROMPT, prices=PRICES, token_limit=10**6)
    trace = [row for row in tools.trace() if row.get("by") != "reviewer"]
    assert len(trace) == len(SPEND) + CLOSING_OVERRUN  # the review in the first of them
    assert tools.conclusions == DRAFT["conclusions"] and tools.claims == []
    assert "did not end the audit in its" in tools.ended_by_harness
    out = tools.audit.out
    assert json.loads((out / "review.json").read_text())["response"] is None
    assert json.loads((out / "claims.json").read_text()) == []
    report = (out / "report.md").read_text()
    assert "ended by the harness with L2's reviewed draft" in report
    writeup = (out / "writeup.md").read_text()
    assert "the harness ended it with its reviewed draft" in writeup and "Draft." in writeup
    assert tools.audit.ended


def test_without_a_review_there_is_no_draft_to_end_with(tmp_path):
    tools = audit_of(tmp_path, 0.003)
    run_l2(tools, fake_model([("list_runs", {})] * 10), PROMPT, prices=PRICES,
           token_limit=10**6)
    assert tools.conclusions is None and tools.ended_by_harness is None
    assert "not ended by L2" in (tools.audit.out / "report.md").read_text()
