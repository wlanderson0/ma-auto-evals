"""Builders for scripted (mockllm) multi-agent runs.

Scripted policies are plain functions of what an agent has seen, so the same
scenario is deterministic across runs and branches. Agents are identified by a
``You are <name>.`` sentence in their system prompt.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from inspect_ai.model import ChatMessage, ModelOutput

MODEL = "mockllm"


def agent_of(messages: list[ChatMessage], names: Iterable[str]) -> str:
    """Return which agent a model request belongs to, from its system prompt."""
    system = next(m.text for m in messages if m.role == "system")
    for name in names:
        if f"You are {name}." in system:
            return name
    raise AssertionError(f"no known agent in system prompt: {system[:200]!r}")


def seen(messages: list[ChatMessage]) -> str:
    """Everything the agent can read in this request, as one string."""
    return "\n".join(m.text for m in messages)


def turns_taken(messages: list[ChatMessage]) -> int:
    """Number of prior assistant generations in this conversation."""
    return sum(1 for m in messages if m.role == "assistant")


def call(function: str, **arguments: Any) -> ModelOutput:
    return ModelOutput.for_tool_call(MODEL, function, arguments)


def post(message: str, channel: str = "team") -> ModelOutput:
    return call("channel_post", channel_id=channel, message=message)


def reply(text: str) -> ModelOutput:
    return ModelOutput.from_content(MODEL, text)


def describe(output: ModelOutput) -> str:
    """Compact human-readable form of a scripted output."""
    calls = output.message.tool_calls
    if calls:
        return f"{calls[0].function}({calls[0].arguments})"
    return output.message.text


def normalize_input(messages: list[ChatMessage]) -> list[tuple[str, str, list]]:
    """ID-free canonical form of a model request, for replay equivalence."""
    out = []
    for m in messages:
        calls = [(c.function, c.arguments) for c in (getattr(m, "tool_calls", None) or [])]
        out.append((m.role, m.text, calls))
    return out
