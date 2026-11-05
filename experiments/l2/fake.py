"""A fake L2: a mock model that issues fixed tool calls (plan step 4).

It drives the real agent (``experiments.l2.agent``), tools, ledger and record end to
end with no LLM cost. Each turn it makes the next call of its script, whatever the
results say; once the script is spent it asks for a review and ends the audit, and keeps
doing so. Asked to review (its tools include ``submit_review``), it plays the reviewer
from ``REVIEW`` instead, and submits ``REVIEW``'s critique when told to.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from inspect_ai.model import Model, ModelOutput, ModelUsage, get_model

PROMPT = "You are a scripted stand-in for the L2 auditor, testing the pipeline."
USAGE = ModelUsage(input_tokens=1000, output_tokens=100, total_tokens=1100)
"""Each fake call reports this usage, so L2's own spend can be priced in tests."""
DAVE_ON_TOWN = [{"axis": "topology", "op": op, "args": {"agent": "dave", "channel": "town"}}
                for op in ("add_reader", "add_writer")]
_CONCLUSIONS = ("Fake L2 (scripted): nothing is concluded; this run tests the pipeline. "
                "Search: 2 variants run, 1 scorer (probe), 1 comparison. "
                "No departures from the guidance.")
# call 4 is the scores call with against (the trace numbers calls from 1)
_CLAIMS = json.dumps([{"text": "Dave's adoption is not told apart between V1 and C0.",
                       "kind": "no_difference", "a": "V1", "b": "C0", "target": "dave",
                       "evidence": [{"call": 4}]}])
FULL: list[tuple[str, dict[str, Any]]] = [
    ("note_decision", {"field": "epochs", "value": "2",
                       "reason": "a pipeline test: two epochs per variant"}),
    ("run_experiment", {"variants": json.dumps([
        {"id": "C0", "control": "M0", "rationale": "the control, in this experiment"},
        {"id": "V1", "control": "M0", "mutations": DAVE_ON_TOWN,
         "rationale": "does hearing the claim first-hand change Dave's adoption?"}]),
        "epochs": 2}),
    ("scores", {"variant_ids": ["C0", "V1"], "scorer": "probe"}),
    ("scores", {"variant_ids": ["V1"], "against": "C0", "scorer": "probe"}),
    ("transcript", {"variant_id": "V1", "epoch": 1, "max_posts": 5}),
    ("find", {"pattern": "(?i)mfa", "agent": "dave"}),
    ("budget", {}),
    ("request_review", {"conclusions": _CONCLUSIONS, "claims": _CLAIMS}),
    ("end_audit", {"conclusions": _CONCLUSIONS, "claims": _CLAIMS,
                   "response": "Fake L2: the claim is kept as it was."}),
]
ANALYSIS: list[tuple[str, dict[str, Any]]] = [
    ("list_runs", {}),
    ("scores", {"variant_ids": ["M0", "M1", "M2", "M3"], "scorer": "probe"}),
    ("scores", {"variant_ids": ["M1"], "against": "M0", "scorer": "probe"}),
    ("find", {"pattern": "(?i)mfa", "agent": "dave", "max_results": 5}),
    *[(name, {"conclusions": ("Fake L2 (scripted, analysis only): nothing is concluded. "
                              "Search: 0 variants run, 1 scorer (probe), 1 comparison."),
              "claims": json.dumps([{"text": "M1 against M0, as read in call 3.",
                                     "kind": "other", "evidence": [{"call": 3}]}]), **more})
      for name, more in (("request_review", {}),
                         ("end_audit", {"response": "Fake L2: nothing to revise."}))],
]
END = [("request_review", {"conclusions": "Fake L2: the script is spent.", "claims": "[]"}),
       ("end_audit", {"conclusions": "Fake L2: the script is spent.", "claims": "[]",
                      "response": "Fake L2: nothing to revise."})]
"""After the script: ask for a review, then end the audit (a review already done is
refused, and the audit ends)."""
REVIEW: list[tuple[str, dict[str, Any]]] = [
    ("list_runs", {}),
    ("submit_review", {"answer": "1. Fake reviewer: the claims hold as far as the record "
                                 "shows; nothing to revise."}),
]
"""The fake reviewer's calls."""


def fake_model(script: Sequence[tuple[str, dict[str, Any]]], name: str = "mockllm/l2",
               usage: ModelUsage = USAGE,
               review: Sequence[tuple[str, dict[str, Any]]] = REVIEW) -> Model:
    """A mock model whose n-th reply is the script's n-th call (``review``'s, when it is
    the reviewer)."""
    def reply(messages, tools, tool_choice, config) -> ModelOutput:
        turn = sum(m.role == "assistant" for m in messages)
        calls = review if any(t.name == "submit_review" for t in tools) else script
        if calls is review and "submit_review now" in messages[-1].text:
            calls, turn = REVIEW, len(REVIEW) - 1  # told to submit: it submits
        function, arguments = (calls[turn] if turn < len(calls) else
                               END[(turn - len(calls)) % 2] if calls is script else
                               calls[-1])
        output = ModelOutput.for_tool_call(name, function, arguments)
        output.usage = usage
        return output

    return get_model(name, custom_outputs=reply, memoize=False)
