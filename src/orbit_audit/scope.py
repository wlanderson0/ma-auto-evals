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

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from orbit_audit.ops import ControlOp, MessageOp, Rollback, op_types


class AuditScope(BaseModel):
    """Operations and agents an audit's policy may use. ``None`` means no limit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ops: frozenset[str] | None = None
    """Operation names the policy may apply, e.g. ``{"replace_generation", "rollback"}``.
    A rollback needs ``"rollback"`` plus the names of its interventions."""
    agents: frozenset[str] | None = None
    """Agents the policy may affect (``ControlOp.agents()``): whose generations
    it writes, whose context it stages, whose deliveries it changes. With a
    limit, message operations must name their recipients: ``recipients=None``
    (every intended recipient) is rejected, since the scope cannot see who
    those are."""

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

    def problem(self, op: ControlOp) -> str | None:
        """Why ``op`` is outside this scope, or ``None``."""
        if not self.allows_op(op.name):
            return (f"{op.name} is outside this audit's scope "
                    f"(allowed operations: {sorted(self.ops or ())})")
        if self.agents is not None:
            if isinstance(op, MessageOp) and getattr(op, "recipients", ()) is None:
                return (f"{op.name} without recipients affects every recipient, "
                        f"which this audit's scope cannot check; name them "
                        f"(allowed agents: {sorted(self.agents)})")
            outside = sorted(set(op.agents()) - self.agents)
            if outside:
                return (f"{op.name} affects {outside}, outside this audit's scope "
                        f"(allowed agents: {sorted(self.agents)})")
        if isinstance(op, Rollback):
            for intervention in op.interventions:
                problem = self.problem(intervention)
                if problem:
                    return f"rollback intervention: {problem}"
        return None

    def describe(self) -> str:
        ops = "any" if self.ops is None else ", ".join(sorted(self.ops))
        agents = "any" if self.agents is None else ", ".join(sorted(self.agents))
        return f"operations: {ops}; agents you may affect: {agents}"
