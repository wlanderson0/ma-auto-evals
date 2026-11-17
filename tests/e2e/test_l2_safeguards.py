"""Replies a model provider's safeguards withhold (issue #55): L2's own, which it is told
of and stopped after several in a row, and the agents', which a launch labels as blocked
rather than as refusals. Offline throughout.
"""

from __future__ import annotations

from types import SimpleNamespace

from inspect_ai.log import read_eval_log, resolve_sample_attachments
from inspect_ai.model import ModelOutput, get_model

from experiments.l2.agent import BLOCKED, run_l2
from experiments.l2.audit import Audit
from experiments.l2.audit_report import write_report
from experiments.l2.experiment import safeguard_blocks
from experiments.l2.fake import PROMPT
from tests.support.l2_audit import brief

PRICES = {"mockllm/l2": (1.0, 2.0)}
IN_A_ROW = 3  # Inspect's react stops an agent after three withheld replies in a row
NOTICE = "Cyber safeguards flagged this message."
FINAL = {"conclusions": "Final: nothing is concluded.", "claims": "[]", "writeup": "Final.",
         "response": "Kept."}
DRAFT = {k: v for k, v in FINAL.items() if k != "response"}


def l2_model(replies):
    """A mock L2: each reply is "blocked" (withheld by its provider) or a (tool, args) call;
    after the list, blocked replies."""
    def reply(messages, tools, tool_choice, config):
        if any(t.name == "submit_review" for t in tools):  # the reviewer submits at once
            return ModelOutput.for_tool_call("mockllm/l2", "submit_review",
                                             {"answer": "1. Fine."})
        turn = sum(m.role == "assistant" for m in messages)
        step = replies[turn] if turn < len(replies) else "blocked"
        if step == "blocked":
            return ModelOutput.from_content("mockllm/l2", NOTICE, stop_reason="content_filter")
        return ModelOutput.for_tool_call("mockllm/l2", *step)
    return get_model("mockllm/l2", custom_outputs=reply, memoize=False)


def audit_for(tmp_path):
    from experiments.l2.tools import Tools

    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    return Tools(audit, on_end=write_report)


def test_l2_is_stopped_after_replies_withheld_in_a_row(tmp_path, offline_module):
    tools = audit_for(tmp_path)
    run_l2(tools, l2_model([]), PROMPT, prices=PRICES)
    assert tools.conclusions is None and tools.trace() == []  # no tool call got through
    assert tools.blocked == {"blocked": IN_A_ROW, "calls": IN_A_ROW, "last_in_a_row": IN_A_ROW}
    report = (tools.audit.out / "report.md").read_text()
    assert "safeguards withheld its last 3 replies in a row" in report  # why it stopped
    assert f"withheld {IN_A_ROW} of its {IN_A_ROW} replies" in report


def test_after_a_withheld_reply_l2_is_told_and_goes_on(tmp_path, offline_module):
    tools = audit_for(tmp_path)
    log = run_l2(tools, l2_model(["blocked", ("request_review", DRAFT), ("end_audit", FINAL)]),
                 PROMPT, prices=PRICES)
    assert tools.audit.ended and tools.conclusions == "Final: nothing is concluded."
    assert tools.blocked["blocked"] == 1
    sample = resolve_sample_attachments(read_eval_log(log.location).samples[0])
    told = [m.text for m in sample.messages if m.role == "user"]
    assert BLOCKED in told  # the next turn says what happened, and not to reword
    assert "withheld 1 of its" in (tools.audit.out / "report.md").read_text()


def event(model, stop_reason):
    choice = SimpleNamespace(stop_reason=stop_reason)
    return SimpleNamespace(event="model", model=model,
                           output=SimpleNamespace(choices=[choice]))


def test_an_agent_s_withheld_replies_are_labelled_not_counted_as_refusals():
    clean = SimpleNamespace(events=[event("m/a", "stop"), event("m/b", "tool_calls")],
                            error=None)
    assert safeguard_blocks(clean) is None
    blocked = SimpleNamespace(
        events=[event("m/a", "stop"), event("m/opus", "content_filter")],
        error=SimpleNamespace(message="ModelGenerateError: ... Cyber safeguards flagged this "
                                      "message. ..."))
    text = safeguard_blocks(blocked)
    assert text.startswith("blocked by the model provider's safeguards")
    assert "1 from m/opus" in text and "the sample's error comes from a blocked reply" in text
    assert "not a refusal" in text
    errored_only = SimpleNamespace(events=[], error=SimpleNamespace(
        message="stop_reason content_filter"))
    assert "error comes from a blocked reply" in safeguard_blocks(errored_only)
    other_error = SimpleNamespace(events=[], error=SimpleNamespace(message="timeout"))
    assert safeguard_blocks(other_error) is None
