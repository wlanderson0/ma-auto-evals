"""The audit controller: one validated path from policy decisions to Orbit.

``AuditController`` implements Orbit's ``RuntimeController``. At each boundary
it builds a ``BoundaryView``, asks its ``AuditPolicy`` for operations,
validates them, applies the valid ones and records every attempt in the
``EvidenceLedger``. A policy never touches Orbit directly (spec §5.5 diagram).

Validation is all-or-nothing per boundary: if any operation in a batch is
invalid, none is applied and each is recorded as rejected. A generation can
be produced by at most one operation. Problems only detectable while applying
(an edit whose text is absent, a schedule revision Orbit rejects) reject that
operation alone, before it has any effect.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from inspect_ai.model import ChatMessage, ModelOutput
from orbit.execution.control import (
    ActivationContext,
    GenerationRequest,
    RuntimeController,
    RuntimeHandles,
)

from orbit_audit.boundaries import Boundary
from orbit_audit.evidence import AUDITOR, EvidenceLedger, auditor_as
from orbit_audit.ops import (
    ActivationOp,
    ControlOp,
    GenerationOp,
    OpError,
    TurnOp,
    summarize,
    tool_schemas,
)
from orbit_audit.policy import AuditPolicy, BoundaryView


@dataclass(frozen=True)
class GenerationTrace:
    """What one agent generation was given, and whether a model saw it."""

    agent: str
    index: int
    turn: int | None
    input: tuple[ChatMessage, ...]
    exposed: bool
    """True when the agent's model received this request (normal or edited path)."""


class AuditController(RuntimeController):
    """Apply an ``AuditPolicy`` to one Orbit sample (or branch)."""

    def __init__(
        self, policy: AuditPolicy, *, actor: str | None = None,
        ledger: EvidenceLedger | None = None,
    ) -> None:
        self.policy = policy
        self.actor = actor or f"orbit_audit:{policy.name}"
        self.ledger = ledger if ledger is not None else EvidenceLedger()
        self.revision = 0
        self.runtime: RuntimeHandles | None = None
        self.turn: int | None = None
        self.generations: list[GenerationTrace] = []
        self._generation_counts: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------ lifecycle
    async def start(self, runtime: RuntimeHandles) -> None:
        self.runtime = runtime
        await self.policy.start()

    async def before_turn(self, turn: int) -> None:
        self.turn = turn

    async def generate(self, request: GenerationRequest, proceed) -> ModelOutput:
        agent = request.agent_name
        index = self._generation_counts[agent]
        self._generation_counts[agent] += 1
        view = self._view(
            Boundary.GENERATION, agent=agent, generation=index,
            input=tuple(m.model_copy(deep=True) for m in request.input),
            tools=tuple(sorted(tool_schemas(request.tools))),
        )
        ops = await self._decide(view, GenerationOp)
        if len(ops) > 1:
            self._reject_all(view, ops, "at most one operation may produce a generation")
            ops = []
        if not ops:
            self._trace(view, exposed=True)
            return await proceed()

        op = ops[0]
        try:
            output, original = await op.produce(request, proceed)
        except OpError as error:
            self._reject(view, op, str(error))
            self._trace(view, exposed=True)
            if error.provider_output is not None:
                return error.provider_output  # the model already ran: deliver it unmodified
            return await proceed()
        self._trace(view, exposed=original is not None)
        self._apply_record(
            view, op, author=auditor_as(agent), original=summarize(original),
            replacement=summarize(output), orbit=_orbit_refs(request),
        )
        return output

    async def before_activation(self, activation: ActivationContext) -> None:
        view = self._view(Boundary.BEFORE_ACTIVATION, agent=activation.agent_name,
                          turn=activation.turn)
        for op in await self._decide(view, ActivationOp):
            try:
                added = op.apply(activation)
            except OpError as error:
                self._reject(view, op, str(error))
                continue
            self._apply_record(view, op, author=AUDITOR, replacement=added)

    async def after_turn(self, turn: int, results: Sequence[Any]) -> None:
        schedule = self.runtime.schedule if self.runtime else None
        view = self._view(
            Boundary.BETWEEN_TURNS, turn=turn,
            schedule_revision=schedule.revision if schedule else None,
            pending_steps=tuple(tuple(s.agents) for s in schedule.pending_steps)
            if schedule else (),
        )
        for op in await self._decide(view, TurnOp):
            try:
                original = op.apply(self.runtime, actor=self.actor)
            except (OpError, ValueError) as error:
                # Orbit validates schedule amendments before mutating them.
                self._reject(view, op, str(error))
                continue
            self._apply_record(view, op, original=original)

    # ------------------------------------------------------------ queries
    def agent_view(self, agent: str, index: int) -> GenerationTrace:
        """The request given to ``agent``'s ``index``-th generation in this run."""
        for trace in self.generations:
            if (trace.agent, trace.index) == (agent, index):
                return trace
        raise KeyError((agent, index))

    # ------------------------------------------------------------ internals
    def _view(self, boundary: Boundary, *, turn: int | None = None, **fields: Any) -> BoundaryView:
        self.revision += 1
        return BoundaryView(
            boundary=boundary, revision=self.revision,
            turn=self.turn if turn is None else turn, **fields,
        )

    async def _decide(self, view: BoundaryView, family: type[ControlOp]) -> list[ControlOp]:
        ops = list(await self.policy.decide(view))
        for op in ops:
            if not isinstance(op, ControlOp):
                raise TypeError(f"{self.policy.name} returned {op!r}, not a ControlOp")
        problems = {id(op): self._problem(view, op, family) for op in ops}
        if any(problems.values()):
            for op in ops:
                self._reject(view, op, problems[id(op)]
                             or "rejected with its batch: another operation was invalid")
            return []
        return ops

    def _problem(self, view: BoundaryView, op: ControlOp, family: type[ControlOp]) -> str | None:
        if view.boundary not in op.boundaries or not isinstance(op, family):
            return f"{op.name} is not allowed at boundary {view.boundary.value}"
        if op.expected_revision is not None and op.expected_revision != view.revision:
            return f"stale: prepared for revision {op.expected_revision}, now {view.revision}"
        known = {spec.name for spec in self.runtime.config.setup.agents} if self.runtime else set()
        unknown = [agent for agent in op.agents() if known and agent not in known]
        if unknown:
            return f"unknown agents {unknown}"
        if view.agent is not None and op.agents() and op.agents() != (view.agent,):
            return f"{op.name} targets {list(op.agents())} but this boundary is {view.agent!r}'s"
        return None

    def _reject(self, view: BoundaryView, op: ControlOp, reason: str) -> None:
        self._record(view, op, outcome="rejected", reason=reason)

    def _reject_all(self, view: BoundaryView, ops: Sequence[ControlOp], reason: str) -> None:
        for op in ops:
            self._reject(view, op, reason)

    def _apply_record(self, view: BoundaryView, op: ControlOp, **fields: Any) -> None:
        self._record(view, op, outcome="applied", reason=op.reason, **fields)

    def _record(self, view: BoundaryView, op: ControlOp, **fields: Any) -> None:
        self.ledger.record(
            op=op.name, op_version=op.version, boundary=view.boundary, actor=self.actor,
            agents=op.agents(), turn=view.turn, revision=view.revision,
            payload={"op": op.name, **op.payload()}, **fields,
        )

    def _trace(self, view: BoundaryView, *, exposed: bool) -> None:
        self.generations.append(GenerationTrace(
            agent=view.agent, index=view.generation, turn=view.turn,
            input=view.input, exposed=exposed,
        ))


def _orbit_refs(request: GenerationRequest) -> dict[str, Any]:
    identity = request.identity
    if identity is None:
        return {}
    return {"session_id": identity.session_id, "invocation_id": identity.invocation_id}

