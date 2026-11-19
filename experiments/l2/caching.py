"""Prompt-cache breakpoints for L2 on Claude through OpenRouter (issue #54).

Inspect 0.3.263 marks only the system prompt and the last tool for caching through
OpenRouter, with the default 5-minute TTL: its per-message marker needs list content and
L2's messages are strings, so the conversation itself is never cached. OpenRouter's
top-level ``cache_control`` adds a trailing breakpoint, but with a longer TTL than
Inspect's (Anthropic wants longer-lived breakpoints first) it did not take effect.

``mark`` places every breakpoint itself, all with one TTL: the last tool, the system
prompt, and the last one or two messages. ``install`` makes a model's requests go
through ``mark``; the model must have Inspect's own markers turned off
(``cache_prompt=False``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from inspect_ai.model import Model

MESSAGE_BREAKPOINTS = 2
"""Breakpoints on the conversation. With the last tool and the system prompt this is 4,
Anthropic's limit. The second one, a message earlier, keeps the previous call's prefix
reachable if the last message is not cacheable."""


def _control(ttl: str | None) -> dict[str, str]:
    return {"type": "ephemeral", "ttl": ttl} if ttl else {"type": "ephemeral"}


def _mark_message(msg: dict[str, Any], ttl: str | None) -> bool:
    """Mark ``msg``'s last content block, turning string content into one text block.
    False if it has no content to mark (e.g. an assistant turn with only tool calls)."""
    content = msg.get("content")
    if isinstance(content, str) and content:
        msg["content"] = [{"type": "text", "text": content, "cache_control": _control(ttl)}]
        return True
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = _control(ttl)
        return True
    return False


def mark(request: dict[str, Any], ttl: str | None, *, tool_messages: bool = True) -> None:
    """Add cache breakpoints to an OpenAI-format request, in place.

    With ``tool_messages=False``, ``tool`` messages keep their string content and the
    conversation breakpoints go on the latest user or assistant messages instead.
    """
    tools = request.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1]["cache_control"] = _control(ttl)
    messages = request.get("messages")
    if not isinstance(messages, list):
        return
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "system":
            _mark_message(msg, ttl)
            break
    left = MESSAGE_BREAKPOINTS
    for msg in reversed(messages):
        if left == 0:
            break
        if not isinstance(msg, dict) or msg.get("role") == "system":
            continue
        if msg.get("role") == "tool" and not tool_messages:
            continue
        if _mark_message(msg, ttl):
            left -= 1


def install(model: Model, marker: Callable[[dict[str, Any]], None]) -> Model:
    """Run ``marker`` on every request ``model`` sends, just before it is sent.

    Wraps the provider's ``_generate_completion`` (Inspect's own hook for its markers;
    private, but Inspect is pinned). Like Inspect's markers, these never appear in the
    request logged to the ``.eval`` file: check the usage's cache reads instead.
    """
    api = model.api
    send = api._generate_completion

    async def marked(request: dict[str, Any], config: Any) -> Any:
        marker(request)
        return await send(request, config)

    api._generate_completion = marked  # type: ignore[method-assign]
    return model
