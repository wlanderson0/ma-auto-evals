"""Cache breakpoints for L2 on Claude through OpenRouter (issue #54)."""

import asyncio
import contextlib

from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    get_model,
)
from inspect_ai.tool import ToolCall, ToolInfo, ToolParams

from experiments.l2 import caching


def _request():
    return {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "brief"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "result 1", "tool_call_id": "c1"},
            {"role": "assistant", "content": "next", "tool_calls": [{"id": "c2"}]},
            {"role": "tool", "content": "result 2", "tool_call_id": "c2"},
        ],
        "tools": [{"type": "function", "function": {"name": "a"}},
                  {"type": "function", "function": {"name": "b"}}],
    }


def _marks(request):
    out = []
    for i, msg in enumerate(request["messages"]):
        if isinstance(msg["content"], list):
            out += [(i, b["cache_control"]) for b in msg["content"] if "cache_control" in b]
    out += [("tool", t["cache_control"]) for t in request["tools"] if "cache_control" in t]
    return out


def test_marks_last_tool_system_and_last_two_messages_with_one_ttl():
    r = _request()
    caching.mark(r, "1h")
    marks = _marks(r)
    assert [where for where, _ in marks] == [0, 4, 5, "tool"]
    assert all(c == {"type": "ephemeral", "ttl": "1h"} for _, c in marks)
    assert len(marks) <= 4  # Anthropic's limit
    assert "cache_control" not in r["tools"][0]
    assert r["messages"][5]["content"] == [
        {"type": "text", "text": "result 2", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    assert r["messages"][1]["content"] == "brief"  # unmarked messages stay strings


def test_without_tool_messages_marks_the_latest_user_or_assistant_text():
    r = _request()
    caching.mark(r, "1h", tool_messages=False)
    # the assistant turn with empty content is skipped; tool results stay strings
    assert [where for where, _ in _marks(r)] == [0, 1, 4, "tool"]
    assert r["messages"][3]["content"] == "result 1"
    assert r["messages"][5]["content"] == "result 2"


def test_no_ttl_gives_the_default_breakpoint():
    r = _request()
    caching.mark(r, None)
    assert all(c == {"type": "ephemeral"} for _, c in _marks(r))


def test_install_marks_the_request_that_is_sent(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")  # the client is made, never called
    model = get_model("openrouter/anthropic/claude-opus-5.5", memoize=False,
                      config=GenerateConfig(cache_prompt=False))
    caching.install(model, lambda request: caching.mark(request, "1h"))
    sent = {}

    async def create(**request):
        sent.update(request)
        raise RuntimeError("not sent")

    model.api.client.chat.completions.create = create
    call = ToolCall(id="c1", function="run", arguments={})
    messages = [ChatMessageSystem(content="sys"), ChatMessageUser(content="brief"),
                ChatMessageAssistant(content="go", tool_calls=[call]),
                ChatMessageTool(content="result", tool_call_id="c1", function="run")]
    tools = [ToolInfo(name="run", description="run", parameters=ToolParams())]
    # the provider's own generate: the offline guard on Model.generate would stop the
    # call before the request is built
    with contextlib.suppress(RuntimeError):
        asyncio.run(model.api.generate(messages, tools, "auto", model.config))
    marks = _marks(sent)
    assert [where for where, _ in marks] == [0, 2, 3, "tool"]
    assert all(c == {"type": "ephemeral", "ttl": "1h"} for _, c in marks)
    assert sent.get("extra_body") is None  # no top-level cache_control
