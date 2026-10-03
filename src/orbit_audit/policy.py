"""Audit policies decide which control operations to apply at each boundary.

The controller asks its policy at every boundary, passing a read-only
``BoundaryView``. A policy can be a script (tests, reproducible audits), a
researcher's Python logic, or an LLM auditor. All of them go through the same
validation and evidence path.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from inspect_ai.model import ChatMessage

from orbit_audit.boundaries import Boundary
from orbit_audit.ops import ControlOp


@dataclass(frozen=True)
class BoundaryView:
    """What the policy sees at one boundary. Copies; editing them changes nothing."""

    boundary: Boundary
    revision: int
    """Pass as ``expected_revision`` to guard an operation against staleness."""
    turn: int | None
    agent: str | None = None
    generation: int | None = None
    """For ``GENERATION``: 0-based index of this agent's generation in the branch."""
    input: tuple[ChatMessage, ...] = ()
    """For ``GENERATION``: the delivered model request."""
    tools: tuple[str, ...] = ()
    """For ``GENERATION``: names of tools offered to the agent."""
    tool_name: str | None = None
    """For ``TOOL_CALL`` / ``TOOL_RESULT``: the called tool."""
    tool_call_id: str | None = None
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None
    """For ``TOOL_RESULT``: what the tool returned (a copy)."""
    schedule_revision: int | None = None
    """For ``BETWEEN_TURNS``: pass to ``AmendPending.schedule_revision``."""
    messages: tuple[dict[str, Any], ...] = ()
    """Canonical channel messages so far (id, channel, sender, recipients, content)."""
    pending_steps: tuple[tuple[str, ...], ...] = ()


class AuditPolicy:
    """Base policy: never intervenes (a passive audit)."""

    name: str = "policy"

    async def decide(self, view: BoundaryView) -> Sequence[ControlOp]:
        return ()

    async def start(self) -> None:
        """Called once per branch before the first boundary."""


Rule = Callable[[BoundaryView], ControlOp | Sequence[ControlOp] | None]


class ScriptedPolicy(AuditPolicy):
    """Apply operations from plain rules; each rule sees every view in order.

    Example::

        ScriptedPolicy(lambda v: ReplaceGeneration(agent="pm", content="ship it")
                       if v.boundary == Boundary.GENERATION and v.agent == "pm"
                       and v.generation == 1 else None)
    """

    name = "scripted"

    def __init__(self, *rules: Rule) -> None:
        self.rules = rules
        self.views: list[BoundaryView] = []

    async def decide(self, view: BoundaryView) -> Sequence[ControlOp]:
        self.views.append(view)
        ops: list[ControlOp] = []
        for rule in self.rules:
            result = rule(view)
            if result is None:
                continue
            ops.extend([result] if isinstance(result, ControlOp) else result)
        return ops


def at_generation(agent: str, index: int, op: ControlOp) -> Rule:
    """Rule: apply ``op`` at ``agent``'s ``index``-th generation (0-based)."""

    def rule(view: BoundaryView) -> ControlOp | None:
        if (view.boundary, view.agent, view.generation) == (Boundary.GENERATION, agent, index):
            return op
        return None

    return rule


def before_activation(agent: str, turn: int, op: ControlOp) -> Rule:
    def rule(view: BoundaryView) -> ControlOp | None:
        if (view.boundary, view.agent, view.turn) == (Boundary.BEFORE_ACTIVATION, agent, turn):
            return op
        return None

    return rule


def after_turn(turn: int, op: ControlOp) -> Rule:
    def rule(view: BoundaryView) -> ControlOp | None:
        if (view.boundary, view.turn) == (Boundary.BETWEEN_TURNS, turn):
            return op
        return None

    return rule


def at_tool(boundary: Boundary, agent: str, tool: str, op: ControlOp, occurrence: int = 0) -> Rule:
    """Rule: apply ``op`` at the ``occurrence``-th call of ``tool`` by ``agent``."""
    seen = 0

    def rule(view: BoundaryView) -> ControlOp | None:
        nonlocal seen
        if (view.boundary, view.agent, view.tool_name) != (boundary, agent, tool):
            return None
        seen += 1
        return op if seen - 1 == occurrence else None

    return rule
