"""The audit controller: one validated path from policy decisions to Orbit.

``AuditController`` implements Orbit's ``RuntimeController``. At each boundary
it builds a ``BoundaryView``, asks its ``AuditPolicy`` for operations,
validates them, applies the valid ones and records every attempt in the
``EvidenceLedger``. A policy never touches Orbit directly (spec §5.5 diagram).

Validation is all-or-nothing per boundary: if any operation in a batch is
invalid, none is applied and each is recorded as rejected. A generation can
be produced by at most one operation. Problems only detectable while applying
(an edit whose text is absent, a schedule revision Orbit rejects) reject that
operation alone, before it has any effect. An optional approver (for example
``auditor.RealismCheck``) vets a valid live batch's content last; its reason
rejects the batch like any other problem.

The controller also records each boundary as a ``branches.Step`` and, when
given a ``Replay``, re-executes a parent branch's prefix instead of asking
the policy (see ``orbit_audit.branches``).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from inspect_ai.model import ChatMessage, ModelOutput
from inspect_ai.tool import ToolError
from orbit.communication import CommunicationState
from orbit.execution.control import (
    ActivationContext,
    DeliveryRequest,
    GenerationRequest,
    RuntimeController,
    RuntimeHandles,
    ToolCallRequest,
)

from orbit_audit.boundaries import Boundary
from orbit_audit.branches import (
    DEFAULT_TOOL_REPLAY,
    AuditLimits,
    BranchEnded,
    BranchIds,
    BranchNode,
    BranchTree,
    BudgetExhausted,
    BudgetLedger,
    Replay,
    ReplayDesync,
    RollbackRequested,
    Step,
    ToolReplay,
    fingerprint,
    fork_problem,
    label,
)
from orbit_audit.delivery import CommunicationWorld, DeliveryOverlay
from orbit_audit.evidence import AUDITOR, EvidenceLedger, agent_author, auditor_as
from orbit_audit.ops import (
    ActivationOp,
    BlockToolCall,
    ControlOp,
    EditGeneration,
    ForceToolCall,
    GenerationOp,
    Halt,
    InjectMessage,
    MessageOp,
    OpError,
    OverrideToolResult,
    ReplaceGeneration,
    RewriteMessage,
    Rollback,
    SimulateToolResult,
    ToolOp,
    TurnOp,
    summarize,
    tool_schemas,
)
from orbit_audit.policy import AuditPolicy, BoundaryView
from orbit_audit.scope import AuditScope

Approver = Callable[[BoundaryView, ControlOp], Awaitable[str | None]]
"""Vets the content of a valid live operation: a rejection reason, or ``None`` to approve."""

_VETTED = (GenerationOp, ActivationOp, ToolOp, RewriteMessage, InjectMessage)
"""Operations that put auditor-written content in front of agents."""

_SOLE = {
    Boundary.GENERATION: "at most one operation may produce a generation",
    Boundary.TOOL_CALL: "at most one operation may decide a tool call",
    Boundary.TOOL_RESULT: "at most one operation may replace a tool result",
}
"""Boundaries whose own operations exclude each other, and the rule as recorded."""


@dataclass(frozen=True)
class GenerationTrace:
    """What one agent generation was given, and whether a model saw it."""

    agent: str
    index: int
    turn: int | None
    input: tuple[ChatMessage, ...]
    tools: tuple[str, ...]
    """Names of the tools offered with the request."""
    exposed: bool
    """True when the agent's model received this request (normal or edited path)."""


class BranchQueries:
    """Read-only projections of a branch's state, for its policy (spec §5.1, §5.4).

    A policy reaches them as ``BoundaryView.queries`` instead of being handed
    every agent's full context at every boundary. Each query returns copies;
    none changes the run, queues an operation or is recorded as evidence.
    """

    def __init__(self, controller: AuditController) -> None:
        self._controller = controller

    def agents(self) -> tuple[str, ...]:
        """The run's agents."""
        runtime = self._controller.runtime
        return tuple(spec.name for spec in runtime.config.setup.agents) if runtime else ()

    def checkpoints(self) -> tuple[tuple[int, str], ...]:
        """``(revision, label)`` of this branch's boundaries so far: the rollback targets."""
        return tuple((step.revision, step.label) for step in self._controller.steps)

    def last_generation(self, agent: str) -> GenerationTrace | None:
        """The request given to ``agent``'s latest generation in this branch, if any."""
        for trace in reversed(self._controller.generations):
            if trace.agent == agent:
                return replace(trace, input=tuple(m.model_copy(deep=True) for m in trace.input))
        return None

    def branches(self) -> tuple[BranchNode, ...]:
        """The audit's branches so far; this one is still running (no status)."""
        return tuple(node.model_copy(deep=True) for node in self._controller.tree.nodes)


class AuditController(RuntimeController):
    """Apply an ``AuditPolicy`` to one Orbit sample (or branch)."""

    def __init__(
        self, policy: AuditPolicy, *, actor: str | None = None,
        ledger: EvidenceLedger | None = None, branch: str = "branch-0",
        branch_index: int = 0, replay: Replay | None = None,
        budget: BudgetLedger | None = None, ids: BranchIds | None = None,
        tool_replay: dict[str, ToolReplay] | None = None,
        scope: AuditScope | None = None, tree: BranchTree | None = None,
        approver: Approver | None = None,
    ) -> None:
        self.policy = policy
        self.scope = scope
        self.approver = approver
        self.budget = budget if budget is not None else BudgetLedger(AuditLimits())
        policy.bind(scope, budget=self.budget)
        self.actor = actor or f"orbit_audit:{policy.name}"
        self.ledger = ledger if ledger is not None else EvidenceLedger()
        self.branch = branch
        self.branch_index = branch_index
        self.replay = replay
        self.ids = ids
        self.tree = tree if tree is not None else BranchTree()
        self.queries = BranchQueries(self)
        self.tool_replay = {**DEFAULT_TOOL_REPLAY, **(tool_replay or {})}
        self.sample_id: int | str | None = None
        self.epoch: int | None = None
        self.steps: list[Step] = []
        self.ending: BranchEnded | None = None
        """Set once the branch must end; every later boundary raises it again."""
        self.revision = 0
        self.runtime: RuntimeHandles | None = None
        self.turn: int | None = None
        self.generations: list[GenerationTrace] = []
        self.overlay = DeliveryOverlay()
        self._generation_counts: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------ lifecycle
    async def start(self, runtime: RuntimeHandles) -> None:
        self.runtime = runtime
        await self.policy.start()

    async def before_turn(self, turn: int) -> None:
        self.turn = turn

    async def generate(self, request: GenerationRequest, proceed) -> ModelOutput:
        self._guard()
        agent = request.agent_name
        index = self._generation_counts[agent]
        self._generation_counts[agent] += 1
        schemas = tool_schemas(request.tools)
        view = self._view(
            Boundary.GENERATION, agent=agent, generation=index,
            input=tuple(m.model_copy(deep=True) for m in request.input),
            tools=tuple(sorted(schemas)),
            tool_specs=tuple(schemas[name] for name in sorted(schemas)),
        )
        proceed = self._proceed(view, proceed)
        ops = await self._decide(view, GenerationOp)
        if not ops:
            self._trace(view, exposed=True)
            return await proceed()

        [op] = ops
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
            replacement=summarize(output),
            orbit={**_orbit_refs(request), "inspect_message_id": output.message.id},
        )
        return output

    async def before_activation(self, activation: ActivationContext) -> None:
        self._guard()
        view = self._view(Boundary.BEFORE_ACTIVATION, agent=activation.agent_name,
                          turn=activation.turn)
        for op in await self._decide(view, ActivationOp):
            try:
                added = op.apply(activation)
            except OpError as error:
                self._reject(view, op, str(error))
                continue
            self._apply_record(view, op, author=AUDITOR, replacement=added)

    async def tool_call(self, call: ToolCallRequest, execute) -> Any:
        self._guard()
        fields = dict(agent=call.agent_name, tool_name=call.function,
                      tool_call_id=call.tool_call_id,
                      tool_arguments=copy.deepcopy(call.arguments))
        refs = {"tool_call_id": call.tool_call_id, **_orbit_refs(call)}
        view = self._view(Boundary.TOOL_CALL, **fields)
        ops = await self._decide(view, ToolOp)
        if ops:
            [op] = ops
            if isinstance(op, BlockToolCall):
                self._apply_record(view, op, author=AUDITOR, replacement=op.message, orbit=refs)
                raise ToolError(op.message)
            if isinstance(op, SimulateToolResult):
                self._apply_record(view, op, author=AUDITOR, replacement=op.result, orbit=refs)
                return op.result
            raise TypeError(f"no tool-call handling for {op.name}")

        step = self.steps[view.revision - 1]
        step.tool, step.executed = call.function, True
        if self.replaying(view) and self.tool_replay.get(call.function) is not ToolReplay.PURE:
            raise self._end(ReplayDesync(
                f"revision {view.revision}: replay would re-execute {call.function!r}, "
                "which is not declared pure"))
        result = await execute()
        view = self._view(Boundary.TOOL_RESULT, tool_result=copy.deepcopy(result), **fields)
        ops = await self._decide(view, ToolOp)
        for op in ops:
            if not isinstance(op, OverrideToolResult):
                raise TypeError(f"no tool-result handling for {op.name}")
            self._apply_record(view, op, author=AUDITOR, original=_jsonable(result),
                               replacement=op.result, orbit=refs)
            return op.result
        return result

    async def after_turn(self, turn: int, results: Sequence[Any]) -> None:
        self._guard()
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

    def deliver(self, request: DeliveryRequest):
        return self.overlay.deliver(request)

    # ------------------------------------------------------------ queries
    def agent_view(self, agent: str, index: int) -> GenerationTrace:
        """The request given to ``agent``'s ``index``-th generation in this run."""
        for trace in self.generations:
            if (trace.agent, trace.index) == (agent, index):
                return trace
        raise KeyError((agent, index))

    def exposure(self, message_id: str) -> dict[str, str]:
        """Recipients whose model actually received a channel message, and the text each saw.

        Spec §5.4: a send names intended recipients and a delivery is a read that returned
        the message (``overlay.delivered_to``), but only the final model request establishes
        exposure. This is Orbit's ``model_exposure`` evidence, matched by message id in a
        request a model answered, after defenses and filters. An unread or dropped message,
        or one in a request the auditor answered itself, exposed no one; a rewrite's
        recipients saw the rewrite.
        """
        state = self.runtime.state.store_as(CommunicationState)
        message = {m.id: m for m in (*state.messages, *self.overlay.injections)}[message_id]
        return {
            event.agent_id: self.overlay.rewrite_for(message_id, event.agent_id) or message.content
            for event in state.events
            if event.kind == "model_exposure" and message_id in event.message_ids
            and event.agent_id != message.sender
        }

    def authors(self) -> dict[str, str]:
        """Authorship label of every channel message in this branch, by message id (P3).

        ``auditor-as:<sender>`` for injected messages and for posts from a generation the
        auditor wrote or edited in the sender's seat; ``agent:<sender>`` otherwise. A post
        is linked to its generation by sender and by the id Orbit recorded for its send:
        the ``channel_post`` tool call, or the output message of a dialogue projection. A
        rewritten post stays its sender's: ``exposure`` says who saw the rewrite.
        Autonomous-behaviour counts take the ``agent:`` messages only.
        """
        state = self.runtime.state.store_as(CommunicationState)
        planted = {(record.agents[0], ref) for record in self.ledger.applied()
                   if record.op in _GENERATION_OPS
                   for ref in (record.orbit["inspect_message_id"],
                               *(call["id"] for call in record.replacement["tool_calls"]))}
        sent_by = {event.message_ids[0]: {(event.agent_id, event.tool_call_id),
                                          (event.agent_id, event.inspect_message_id)}
                   for event in state.events if event.kind == "sent"}
        labels = {m.id: (auditor_as if sent_by[m.id] & planted else agent_author)(m.sender)
                  for m in state.messages}
        return labels | {m.id: auditor_as(m.sender) for m in self.overlay.injections}

    def replaying(self, view: BoundaryView) -> bool:
        """True while re-executing the parent's prefix (before the fork point)."""
        return self.replay is not None and view.revision < self.replay.fork_revision

    # ------------------------------------------------------------ internals
    def _guard(self) -> None:
        if self.ending is not None:
            raise self.ending

    def _end(self, error: BranchEnded) -> BranchEnded:
        self.ending = error
        return error

    def _view(self, boundary: Boundary, *, turn: int | None = None, **fields: Any) -> BoundaryView:
        self.revision += 1
        view = BoundaryView(
            boundary=boundary, revision=self.revision,
            turn=self.turn if turn is None else turn,
            messages=CommunicationWorld.current().summary(), branch=self.branch,
            queries=self.queries, **fields,
        )
        step = Step(revision=view.revision, boundary=boundary, agent=view.agent, turn=view.turn,
                    fingerprint=fingerprint(view), label=label(view))
        self.steps.append(step)
        replay = self.replay
        if replay is not None and view.revision <= replay.fork_revision:
            recorded = replay.step(view.revision)
            if recorded.fingerprint != step.fingerprint:
                raise self._end(ReplayDesync(
                    f"revision {view.revision}: replay reached {step.label} "
                    f"but {replay.parent} recorded {recorded.label} with different content"))
            if view.revision == replay.fork_revision and self.ids is not None:
                self.ids.diverge(self.branch_index)  # from here on, this branch's own IDs
        return view

    def _proceed(self, view: BoundaryView, proceed):
        """``proceed`` for this generation: replayed, or live and budgeted."""
        step = self.steps[view.revision - 1]
        if self.replaying(view):
            recorded = iter(self.replay.step(view.revision).outputs)

            async def replayed(*, input=None) -> ModelOutput:
                output = next(recorded, None)
                if output is None:
                    raise self._end(ReplayDesync(
                        f"revision {view.revision}: more model calls than {self.replay.parent}"))
                output = await proceed(input=input, replay=output)
                step.outputs.append(output.model_copy(deep=True))
                return output

            return replayed

        async def live(*, input=None) -> ModelOutput:
            spent = self.budget.exhausted()
            if spent:
                raise self._end(BudgetExhausted(spent))
            output = await proceed(input=input)
            self.budget.spend(output)
            step.outputs.append(output.model_copy(deep=True))
            return output

        return live

    async def _decide(self, view: BoundaryView, family: type[ControlOp]) -> list[ControlOp]:
        """Validate the batch for this boundary; apply message ops; return ``family`` ops.

        The batch comes from the policy, or, while replaying, from the parent's
        recording; at the fork revision it is the rollback's interventions
        followed by whatever the policy adds.
        """
        replay = self.replay
        if replay is not None and view.revision < replay.fork_revision:
            ops = list(replay.step(view.revision).decisions)
        else:
            try:
                ops = list(await self.policy.decide(view))
            except BranchEnded as ended:  # e.g. the policy's own model budget is spent
                self._end(ended)
                raise
            if replay is not None and view.revision == replay.fork_revision:
                ops = [*replay.interventions, *ops]
        for op in ops:
            if not isinstance(op, ControlOp):
                raise TypeError(f"{self.policy.name} returned {op!r}, not a ControlOp")
        self.steps[view.revision - 1].decisions = tuple(ops)
        rollbacks = [op for op in ops if isinstance(op, Rollback)]
        if rollbacks:
            self._rollback(view, ops, rollbacks)
            return []
        problems = {id(op): self._problem(view, op, family) for op in ops}
        producers = [op for op in ops if isinstance(op, family)]
        if len(producers) > 1 and view.boundary in _SOLE and not any(problems.values()):
            problems.update((id(op), _SOLE[view.boundary]) for op in producers)
        if spent := self._over_budget(view, ops):
            problems.update({id(op): problems[id(op)] or spent for op in ops if _counted(op)})
        if not any(problems.values()) and (veto := await self._veto(view, ops)):
            problems.update(veto)
            # Nothing was applied, so record no decisions: a replay applies none without
            # asking. Unlike other rejections, the vetoed attempt is then not re-recorded
            # in child branches; only this branch's ledger holds it.
            self.steps[view.revision - 1].decisions = ()
        if any(problems.values()):
            for op in ops:
                self._reject(view, op, problems[id(op)]
                             or "rejected with its batch: another operation was invalid")
            return []
        for op in ops:
            if isinstance(op, MessageOp):
                self._apply_message(view, op)
        return [op for op in ops if not isinstance(op, MessageOp)]

    def _rollback(
        self, view: BoundaryView, ops: list[ControlOp], rollbacks: list[Rollback],
    ) -> None:
        if self.replaying(view):
            self._reject_all(view, ops, "rollbacks are not re-applied during replay")
            return
        if len(ops) > 1:
            self._reject_all(view, ops, "a rollback must be the only operation in its batch")
            return
        [op] = rollbacks
        problem = self._rollback_problem(view, op)
        if problem:
            self._reject(view, op, problem)
            return
        target = self.steps[op.to_revision - 1]
        self._apply_record(view, op, details={"to_revision": op.to_revision,
                                              "fork_boundary": target.label})
        raise self._end(RollbackRequested(op))

    def _rollback_problem(self, view: BoundaryView, op: Rollback) -> str | None:
        if self.scope is not None and (problem := self.scope.problem(op)):
            return problem
        if op.expected_revision is not None and op.expected_revision != view.revision:
            return f"stale: prepared for revision {op.expected_revision}, now {view.revision}"
        if op.to_revision > view.revision:
            return f"cannot roll forward to revision {op.to_revision} (now {view.revision})"
        target = self.steps[op.to_revision - 1]
        for intervention in op.interventions:
            if target.boundary not in intervention.boundaries:
                return (f"{intervention.name} is not allowed at revision {op.to_revision} "
                        f"({target.label})")
        config = self.runtime.config if self.runtime else None
        if config is not None and any(defense.enabled for defense in config.defenses):
            return "rollback is not supported for samples with defenses or model filters yet"
        limits = self.budget.limits
        if self.budget.branches >= limits.max_branches:
            return f"branch budget spent ({self.budget.branches}/{limits.max_branches})"
        if spent := self.budget.intervention_problem(sum(map(_counted, op.interventions))):
            return f"rollback interventions: {spent}"
        return fork_problem(tuple(self.steps), self.tool_replay)

    def _over_budget(self, view: BoundaryView, ops: list[ControlOp]) -> str | None:
        """Why this batch does not fit ``max_interventions``, or ``None``.

        A replayed batch gets its parent's verdict: its operations were counted
        there, and the count only grows, so checking today's count could reject
        what the parent applied and desync the child.
        """
        step = self.steps[view.revision - 1]
        if self.replaying(view):
            step.over_budget = self.replay.step(view.revision).over_budget
        else:
            step.over_budget = self.budget.intervention_problem(sum(map(_counted, ops)))
        return step.over_budget

    def _apply_message(self, view: BoundaryView, op: MessageOp) -> None:
        try:
            fields = op.apply(self.overlay, CommunicationWorld.current())
        except OpError as error:
            self._reject(view, op, str(error))
            return
        self._apply_record(view, op, **fields)

    def _problem(self, view: BoundaryView, op: ControlOp, family: type[ControlOp]) -> str | None:
        if self.scope is not None and (problem := self.scope.problem(op)):
            return problem
        if view.boundary not in op.boundaries or not isinstance(op, family | MessageOp):
            return f"{op.name} is not allowed at boundary {view.boundary.value}"
        if op.expected_revision is not None and op.expected_revision != view.revision:
            return f"stale: prepared for revision {op.expected_revision}, now {view.revision}"
        known = {spec.name for spec in self.runtime.config.setup.agents} if self.runtime else set()
        unknown = [agent for agent in op.agents() if known and agent not in known]
        if unknown:
            return f"unknown agents {unknown}"
        targeted = not isinstance(op, MessageOp)
        if targeted and view.agent is not None and op.agents() and op.agents() != (view.agent,):
            return f"{op.name} targets {list(op.agents())} but this boundary is {view.agent!r}'s"
        pinned = getattr(op, "tool_call_id", None)
        if pinned is not None and pinned != view.tool_call_id:
            return f"{op.name} is for tool call {pinned!r}, not {view.tool_call_id!r}"
        return None

    async def _veto(self, view: BoundaryView, ops: list[ControlOp]) -> dict[int, str]:
        """The approver's reason against a valid batch, keyed like ``_decide``'s problems.

        Only live content is vetted: a replayed prefix is not live (R3), while
        a rollback's interventions at the fork revision are.
        """
        if self.approver is None or self.replaying(view):
            return {}
        for op in ops:
            if isinstance(op, _VETTED) and (reason := await self.approver(view, op)):
                return {id(op): reason}
        return {}

    def _reject(self, view: BoundaryView, op: ControlOp, reason: str) -> None:
        self._record(view, op, outcome="rejected", reason=reason)

    def _reject_all(self, view: BoundaryView, ops: Sequence[ControlOp], reason: str) -> None:
        for op in ops:
            self._reject(view, op, reason)

    def _apply_record(self, view: BoundaryView, op: ControlOp, **fields: Any) -> None:
        if _counted(op) and not self.replaying(view):
            self.budget.interventions += 1
        self._record(view, op, outcome="applied", reason=op.reason, **fields)

    def _record(self, view: BoundaryView, op: ControlOp, **fields: Any) -> None:
        record = self.ledger.record(
            op=op.name, op_version=op.version, boundary=view.boundary, actor=self.actor,
            agents=op.agents(), turn=view.turn, revision=view.revision, branch=self.branch,
            replayed=self.replaying(view),
            payload={"op": op.name, **op.payload()}, **fields,
        )
        self.policy.notice(record)

    def _trace(self, view: BoundaryView, *, exposed: bool) -> None:
        self.generations.append(GenerationTrace(
            agent=view.agent, index=view.generation, turn=view.turn,
            input=view.input, tools=view.tools, exposed=exposed,
        ))


_GENERATION_OPS = frozenset(op.name for op in (ReplaceGeneration, EditGeneration, ForceToolCall))


def _counted(op: ControlOp) -> bool:
    """Whether ``op`` uses the intervention budget (see ``AuditLimits.max_interventions``)."""
    return not isinstance(op, Halt | Rollback)


def _jsonable(value: Any) -> Any:
    if isinstance(value, str | int | float | bool | None):
        return value
    try:
        return [item.model_dump(mode="json") for item in value]
    except (TypeError, AttributeError):
        return repr(value)


def _orbit_refs(request: GenerationRequest | ToolCallRequest) -> dict[str, Any]:
    identity = request.identity
    if identity is None:
        return {}
    return {"session_id": identity.session_id, "invocation_id": identity.invocation_id}

