"""Regressions found in the first live audits (deterministic parts only)."""

from __future__ import annotations

import asyncio

from inspect_ai.model import ModelOutput, get_model
from inspect_ai.tool import ToolDef

from experiments.run import intended_posts, judge_verdict
from orbit_audit.auditor.llm import _TOOL_SIGNATURES


def test_auditor_tool_schemas_are_strict_mode_compatible():
    """OpenAI-compatible providers send tools with strict=true: every property must
    be required and there may be no free-form objects."""
    for name, fn in _TOOL_SIGNATURES.items():
        params = ToolDef(fn, name=name).parameters
        assert set(params.properties) == set(params.required), name
        assert all(p.type != "object" for p in params.properties.values()), name


def test_tool_calls_written_as_json_text_are_recovered():
    text = ('I will post my decision.\n{"type": "function", "name": "channel_post", '
            '"parameters": {"channel_id": "team", "message": "REJECT fix #123: \\"3 failed\\""}}')
    assert intended_posts(text) == ['REJECT fix #123: \\"3 failed\\"']


def _judge(answer: str):
    return get_model("mockllm/judge", custom_outputs=lambda *a: ModelOutput.from_content(
        "mockllm", answer), memoize=False)


def test_verdict_reads_the_intended_post_before_asking_the_judge():
    text = '{"name": "channel_post", "parameters": {"message": "APPROVE fix #123 today."}}'
    assert asyncio.run(judge_verdict(_judge("REJECT"), [text])) == "APPROVE"


def test_negated_decisions_go_to_the_judge():
    assert asyncio.run(judge_verdict(_judge("**REJECT**"), ["I cannot approve this."])) == "REJECT"
    assert asyncio.run(judge_verdict(_judge("NONE"), ["Let me read the channel."])) is None
