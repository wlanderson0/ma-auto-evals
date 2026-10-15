"""What an audit's policy may do: its brief, enforced by the controller.

Telling an LLM auditor "only act through Alice" is a prompt, and prompts leak:
in the first live audits the auditor tried a forbidden ``override_tool_result``
twice. An ``AuditScope`` turns the brief into validation. The controller
rejects, with a recorded reason, any operation whose name is not allowed or
that affects an agent outside the allowed set, including the interventions
carried by a ``Rollback``. Scope checks run before every other check, so an
out-of-scope attempt is always reported as such.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, field_validator

from orbit_audit.ops import ControlOp, Rollback, op_types

if TYPE_CHECKING:
    from orbit_audit.delivery import CommunicationWorld


class AuditScope(BaseModel):
    """Operations and agents an audit's policy may use. ``None`` means no limit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ops: frozenset[str] | None = None
    """Operation names the policy may apply, e.g. ``{"replace_generation", "rollback"}``.
    A rollback needs ``"rollback"`` plus the names of its interventions."""
    agents: frozenset[str] | None = None
    """Agents the policy may affect (``ControlOp.affected()``): whose generations
    it writes, whose context it stages, whose deliveries it changes."""

    @field_validator("ops", "agents", mode="before")
    @classmethod
    def _freeze(cls, value: Any) -> Any:
        return None if value is None else frozenset(value)

    @field_validator("ops")
    @classmethod
    def _known(cls, ops: frozenset[str] | None) -> frozenset[str] | None:
        unknown = sorted(set(ops or ()) - set(op_types()))
        if unknown:
            raise ValueError(f"unknown control operations {unknown}")
        return ops

    def allows_op(self, name: str) -> bool:
        return self.ops is None or name in self.ops

    def problem(self, op: ControlOp, world: CommunicationWorld | None = None) -> str | None:
        """Why ``op`` is outside this scope, or ``None``.

        ``world`` resolves a message op that leaves its recipients implicit
        (``recipients=None``) to every recipient it would reach.
        """
        if not self.allows_op(op.name):
            return (f"{op.name} is outside this audit's scope "
                    f"(allowed operations: {sorted(self.ops or ())})")
        if self.agents is not None:
            outside = sorted(set(op.affected(world)) - self.agents)
            if outside:
                return (f"{op.name} affects {outside}, outside this audit's scope "
                        f"(allowed agents: {sorted(self.agents)})")
        if isinstance(op, Rollback):
            for intervention in op.interventions:
                problem = self.problem(intervention, world)
                if problem:
                    return f"rollback intervention: {problem}"
        return None

    def describe(self) -> str:
        ops = "any" if self.ops is None else ", ".join(sorted(self.ops))
        agents = "any" if self.agents is None else ", ".join(sorted(self.agents))
        return f"operations: {ops}; agents you may affect: {agents}"
