"""The adversarial reviewer of L2's draft (spec §6.5; L2-21).

The fake L2 asks for a review and ends the audit; the same fake model plays the reviewer
(``fake.REVIEW``) when its tools include ``submit_review``. Offline throughout.
"""

from __future__ import annotations

import json

import anyio
import pytest
from inspect_ai.log import read_eval_log, resolve_sample_attachments

from experiments.l2.agent import REVIEW_MESSAGES, REVIEW_SHARE, REVIEWER_PROMPT, run_l2
from experiments.l2.audit import Audit
from experiments.l2.audit_report import render, write_report
from experiments.l2.fake import FULL, PROMPT, REVIEW, fake_model
from experiments.l2.tools import READ, Tools
from tests.support.l2_audit import brief

PRICES = {"mockllm/l2": (1.0, 2.0)}  # $0.0012 per fake call
DRAFT = {"conclusions": "Draft: nothing is concluded.", "claims": "[]", "writeup": "Draft."}
FINAL = {"conclusions": "Final: nothing is concluded.", "claims": "[]", "writeup": "Final."}


@pytest.fixture(scope="module")
def reviewed(tmp_path_factory, offline_module):
    folder = tmp_path_factory.mktemp("review")
    audit = Audit.create(brief(folder, 1.0), folder / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    log = run_l2(tools, fake_model(FULL), PROMPT, prices=PRICES)
    return tools, log


def model_events(log):
    sample = resolve_sample_attachments(read_eval_log(log.location).samples[0])
    return [e for e in sample.events if e.event == "model"]


def test_the_review_is_recorded_with_the_draft_the_critique_and_the_response(reviewed):
    tools, _ = reviewed
    review = json.loads((tools.audit.out / "review.json").read_text())
    trace = {row["seq"]: row for row in tools.trace()}
    assert trace[review["call"]]["tool"] == "request_review"
    [call] = review["reviewer_calls"]
    assert trace[call]["tool"] == "list_runs" and trace[call]["by"] == "reviewer"
    assert review["critique"] == REVIEW[-1][1]["answer"]
    assert review["response"] == FULL[-1][1]["response"]
    [claim] = review["draft"]["claims"]  # the draft's claims, with the checker's verdicts
    assert claim["kind"] == "no_difference" and claim["verdict"] and claim["reasons"]
    assert review["cost_usd"] == pytest.approx(len(REVIEW) * 0.0012)
    assert review["stopped"] is None


def test_the_report_shows_the_critique_and_the_response(reviewed):
    tools, _ = reviewed
    report = (tools.audit.out / "report.md").read_text()
    review = report[report.index("## Review"):report.index("## Decisions")]
    assert "> " + REVIEW[-1][1]["answer"] in review
    assert "> " + FULL[-1][1]["response"] in review
    assert "Dave's adoption is not told apart between V1 and C0." in review  # the draft
    assert "made 1 call(s)" in review
    assert "the reviewer's: $0.0024" in report  # in the ledger
    assert "1 by the reviewer" in report  # in the search counts
    assert "`review.json`" in report


def test_the_reviewer_sees_the_draft_with_verdicts_and_only_the_query_tools(reviewed):
    _, log = reviewed
    events = [e for e in model_events(log)
              if any(t.name == "submit_review" for t in e.tools)]
    assert len(events) == len(REVIEW)  # its calls are in L2's own log (and so the ledger)
    first = events[0]
    assert {t.name for t in first.tools} == {*READ, "submit_review"}
    system = "\n".join(m.text for m in first.input if m.role == "system")
    user = "\n".join(m.text for m in first.input if m.role == "user")
    assert REVIEWER_PROMPT.splitlines()[0] in system and "# How to read results" in system
    assert "# The brief" in user and "# L2's draft conclusions" in user
    assert "# L2's draft claims, with the checker's verdicts" in user
    assert "(no_difference: a=V1, b=C0, target=dave)" in user and "Evidence: call 4" in user
    assert "your own calls" not in user  # the budget lines are L2's


def test_l2_is_offered_request_review(reviewed):
    _, log = reviewed
    l2 = [e for e in model_events(log) if not any(t.name == "submit_review" for t in e.tools)]
    assert {"request_review", "end_audit"} <= {t.name for t in l2[0].tools}


# ------------------------------------------------------------ the rules, on the tools
def stub(critique="1. A point."):
    async def review(tools, draft):
        review.draft = draft
        return {"critique": critique, "calls": [], "cost_usd": 0.0, "stopped": None}
    return review


def reviewing(tmp_path, budget=1.0):
    audit = Audit.create(brief(tmp_path, budget), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    tools.needs_review = True
    return tools


def test_end_audit_waits_for_the_review_then_needs_a_response(tmp_path):
    tools = reviewing(tmp_path)
    ok, text = tools.call("end_audit", **FINAL)
    assert not ok and "first call request_review" in text
    review = stub()
    ok, text, _ = anyio.run(lambda: tools.reviewed_call(review, **DRAFT))
    assert ok and "1. A point." in text and "revise once" in text
    assert review.draft == {"conclusions": "Draft: nothing is concluded.", "claims": [],
                            "writeup": "Draft."}
    ok, text = tools.call("end_audit", **FINAL)
    assert not ok and "response is required" in text and not tools.audit.ended
    ok, text = tools.call("end_audit", **FINAL, response="Kept: nothing to change.")
    assert ok and tools.audit.ended
    saved = json.loads((tools.audit.out / "review.json").read_text())
    assert saved["response"] == "Kept: nothing to change."
    assert saved["draft"]["conclusions"] == "Draft: nothing is concluded."


def test_after_the_review_l2_only_revises(tmp_path):
    """No new experiment, scenario or second review once the review is done; the query
    tools still work."""
    tools = reviewing(tmp_path)
    assert anyio.run(lambda: tools.reviewed_call(stub(), **DRAFT))[0]
    for name, args in (("run_experiment", {"variants": "[]", "epochs": 1, "rationale": "a test launch"}),
                       ("write_scenario", {"name": "x", "code": "pass"})):
        ok, text = tools.call(name, **args)
        assert not ok and "the review is done" in text, name
    ok, text, _ = anyio.run(lambda: tools.reviewed_call(stub(), **DRAFT))
    assert not ok and "the review is done" in text
    assert tools.call("list_runs")[0] and tools.call("budget")[0]


def test_a_malformed_draft_is_refused_and_nothing_is_reviewed(tmp_path):
    tools = reviewing(tmp_path)
    review = stub()
    ok, text, _ = anyio.run(lambda: tools.reviewed_call(
        review, conclusions="Draft.", claims=[{"kind": "rate", "text": "x"}]))
    assert not ok and "could not be recorded" in text
    assert tools.review is None and not hasattr(review, "draft")
    assert not (tools.audit.out / "review.json").exists()


def test_the_reviewer_reads_only_and_the_hard_stop_does_not_refuse_it(tmp_path):
    tools = reviewing(tmp_path, budget=1.0)
    for name in ("run_experiment", "write_scenario", "run_python", "note_decision",
                 "end_audit", "request_review", "budget"):
        ok, text, _ = tools.numbered_call(name, "reviewer")
        assert not ok and "the reviewer can use only" in text, name
    tools.audit.set_l2_spend({"usd": 0.95, "by_model": {}})  # past the hard stop
    assert not tools.call("list_runs")[0]  # L2 is refused
    ok, text, seq = tools.numbered_call("list_runs", "reviewer")
    assert ok, text
    assert next(r for r in tools.trace() if r["seq"] == seq)["by"] == "reviewer"


def test_request_review_without_a_reviewer_is_refused(tmp_path):
    tools = Tools(Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True))
    ok, text = tools.call("request_review", **DRAFT)
    assert not ok and "needs the reviewer" in text
    assert tools.call("end_audit", **FINAL)[0]  # no reviewer: end_audit ends at once


# ---------------------------------------------------------- the reviewer's bounds
def test_the_reviewer_stops_at_its_share_of_what_is_left(tmp_path):
    """A reviewer that never submits is stopped once it has spent its share (a quarter) of
    what was left;
    L2 still revises and ends the audit."""
    audit = Audit.create(brief(tmp_path, 0.05), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    script = [("request_review", DRAFT), ("end_audit", {**FINAL, "response": "Kept."})]
    run_l2(tools, fake_model(script, review=[("list_runs", {})]), PROMPT, prices=PRICES,
           token_limit=10**6)
    review = tools.review
    assert "share of the budget" in review["stopped"]
    cap = REVIEW_SHARE * (0.05 - 0.0012)  # of what was left after L2's first call
    assert REVIEW_SHARE == 0.25
    # at most one call over, then one last turn, in which it submits what it has
    assert cap <= review["cost_usd"] <= cap + 2 * 0.0012 + 1e-9
    assert review["critique"] == REVIEW[-1][1]["answer"]
    assert tools.conclusions == "Final: nothing is concluded." and audit.ended


def test_the_reviewer_has_a_message_limit(tmp_path):
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    script = [("request_review", DRAFT), ("end_audit", {**FINAL, "response": "Kept."})]
    run_l2(tools, fake_model(script, review=[("list_runs", {})]), PROMPT,
           prices={"mockllm/l2": (0.0, 0.0)})
    assert f"message limit ({REVIEW_MESSAGES})" in tools.review["stopped"]
    assert audit.ended


def test_a_review_with_no_response_says_so(tmp_path):
    tools = reviewing(tmp_path)
    assert anyio.run(lambda: tools.reviewed_call(stub(""), **DRAFT))[0]
    report = render(tools, "L2 spent the whole budget before calling end_audit")
    assert "The reviewer gave no critique." in report
    assert "None: L2 did not end the audit after the review." in report


def test_no_review_requested_is_said_when_there_is_a_reviewer(tmp_path):
    tools = reviewing(tmp_path)
    assert "No review was requested" in render(tools, "stopped")
    tools.needs_review = False
    assert "## Review" not in render(tools, "stopped")


def test_a_failing_reviewer_is_recorded_and_the_audit_can_still_end(tmp_path):
    tools = reviewing(tmp_path)

    async def broken(tools, draft):
        raise RuntimeError("the provider said 400")

    ok, text, _ = anyio.run(lambda: tools.reviewed_call(broken, **DRAFT))
    assert ok and "the reviewer failed: RuntimeError: the provider said 400" in text
    assert tools.review["critique"] == "" and "failed" in tools.review["stopped"]
    assert tools.call("end_audit", **FINAL, response="No critique to answer.")[0]


def test_the_reviewer_s_comparisons_are_not_l2_s_search(tmp_path):
    """A comparison the reviewer makes neither counts in L2's search counts and tables nor
    tightens the correction on L2's claims; L2 can still cite it."""
    from experiments.l2 import claims as claims_
    from tests.support.l2_audit import InProcess

    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True,
                         launch=InProcess())
    tools = Tools(audit, on_end=write_report)
    c0 = {"id": "C0", "control": "M0", "rationale": "control"}
    dave = [{"axis": "topology", "op": op, "args": {"agent": "dave", "channel": "town"}}
            for op in ("add_reader", "add_writer")]
    v1 = {"id": "V1", "control": "M0", "rationale": "Dave on #town", "mutations": dave}
    v2 = {"id": "V2", "control": "M0", "rationale": "the same again", "mutations": dave}
    assert tools.call("run_experiment", variants=[c0, v1, v2], epochs=1, rationale="a test")[0]
    assert tools.call("scores", variant_ids=["V1"], against="C0", scorer="probe")[0]
    ok, _, seq = tools.numbered_call("scores", "reviewer", variant_ids=["V2"], against="C0",
                                     scorer="probe")
    assert ok and len(claims_._all_scores(tools)) == 1
    tools.needs_review = False
    claim = {"text": "No difference for dave.", "kind": "no_difference", "a": "V2", "b": "C0",
             "target": "dave", "evidence": [{"call": seq}]}
    assert tools.call("end_audit", conclusions="None.", claims=[claim])[0]  # citable
    report = (audit.out / "report.md").read_text()
    assert "- Comparisons tried: 1 distinct (1 successful calls)" in report
    assert "### compare V2 vs C0" not in report
    assert "- Tool calls: 2 (0 refused or failed), and 1 by the reviewer" in report


def test_end_audit_says_which_calls_are_evidence(tmp_path):
    """In a live run L2 cited its request_review call for a point the reviewer raised."""
    from inspect_ai.tool import ToolDef

    from experiments.l2.claims import CITABLE
    from experiments.l2.tools import inspect_tools

    tools = reviewing(tmp_path)
    [end] = [ToolDef(t) for t in inspect_tools(tools, review=stub())
             if ToolDef(t).name == "end_audit"]
    described = " ".join(end.parameters.properties["claims"].description.split())
    assert all(name in described for name in CITABLE)
    assert "request_review (the critique) is not" in described
