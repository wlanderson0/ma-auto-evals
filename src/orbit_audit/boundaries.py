"""Lifecycle boundaries where an auditor may act (``docs/spec.md`` §3.2)."""

from __future__ import annotations

from enum import StrEnum


class Boundary(StrEnum):
    """A point in an Orbit run at which control operations may be applied."""

    GENERATION = "generation"
    """§3.2 (1): during an agent's generation. Let it run, edit it, or write it."""

    BEFORE_ACTIVATION = "before_activation"
    """§3.2 (2): before a scheduled agent activation. Stage non-agent input."""

    TOOL_CALL = "tool_call"
    """§3.2 (3): between a generation and tool execution. Requires Orbit seam S4."""

    TOOL_RESULT = "tool_result"
    """§3.2 (4): after tool execution, before exposure. Requires Orbit seam S4."""

    BETWEEN_TURNS = "between_turns"
    """§3.2 (5): between outer turns. Change pending activations or halt."""
