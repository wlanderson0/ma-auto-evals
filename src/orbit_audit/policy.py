"""Audit policies decide which control operations to apply at each boundary.

The controller asks its policy at every boundary, passing a read-only
``BoundaryView``. A policy can be a script (tests, reproducible audits), a
researcher's Python logic, or an LLM auditor. All of them go through the same
validation and evidence path.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from inspect_ai.model import ChatMessage, Model, ModelOutput
from inspect_ai.tool import ToolInfo

from orbit_audit.boundaries import Boundary
from orbit_audit.branches import BudgetExhausted
from orbit_audit.evidence import InterventionRecord
from orbit_audit.ops import ControlOp

if TYPE_CHECKING:
    from orbit_audit.branches import BudgetLedger
    from orbit_audit.controller import BranchQueries
    from orbit_audit.scope import AuditScope


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
    tool_specs: tuple[ToolInfo, ...] = ()
    """For ``GENERATION``: schemas of the tools offered to the agent."""
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
    branch: str = "branch-0"
    """The branch this view belongs to; revisions are per branch."""
    queries: BranchQueries | None = field(default=None, compare=False, repr=False)
    """Read-only queries over the branch's live state (checkpoints, agent inputs, branches)."""


class AuditPolicy:
    """Base policy: never intervenes (a passive audit)."""

    name: str = "policy"
    scope: AuditScope | None = None
    """The audit's scope, set by the controller; operations outside it are rejected."""
    budget: BudgetLedger | None = None
    """The audit's budget, set by the controller; ``generate`` charges it."""

    async def decide(self, view: BoundaryView) -> Sequence[ControlOp]:
        return ()

    def bind(self, scope: AuditScope | None, budget: BudgetLedger | None = None) -> None:
        """Called by the controller with the audit's scope and budget before any boundary."""
        self.scope = scope
        self.budget = budget

    async def generate(self, model: Model, input: list[ChatMessage], **options: Any) -> ModelOutput:
        """Call the policy's own model, charged to the audit budget (spec §5.6).

        Once ``max_auditor_calls`` is spent this raises ``BudgetExhausted``, which
        ends the branch, instead of calling. An unbound policy is not charged.
        """
        if self.budget is not None and (spent := self.budget.auditor_exhausted()):
            raise BudgetExhausted(spent)
        output = await model.generate(input, **options)
        if self.budget is not None:
            self.budget.spend_auditor(output)
        return output

    async def start(self) -> None:
        """Called once per branch before the first boundary."""

    def notice(self, record: InterventionRecord) -> None:
        """Called with the outcome of every operation this policy's controller tried."""


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
