"""Run one audited sample as a tree of isolated branches.

Each branch runs Orbit's ``mas_orchestrator`` from the sample's initial state
in its own asyncio task, with a copied context, a fresh Inspect ``Store`` and
deterministic Orbit identifiers, inside ``span(type="branch")``. Branches
therefore never see each other's channel messages, agent conversations or
store-backed tool effects (R6), and the sample's own store is untouched until
the end.

The baseline branch runs first. When a branch ends with a rollback, its child
is queued; the audit ends when no branch is pending. The sample then adopts
the state of its last completed branch (or, if none completed, its last
branch), so Orbit's scorers score the final timeline, and the branch tree and
budget are stored under ``orbit_audit:branches`` and ``orbit_audit:budget``.
Each node keeps its branch's final channel messages, so abandoned branches
remain evidence.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import LimitExceededError, span, store
from inspect_ai.util._store import init_subtask_store
from orbit.ids import id_scope
from orbit.solvers.orchestrator import mas_orchestrator

from orbit_audit.branches import (
    AuditLimits,
    BranchEnded,
    BranchIds,
    BranchStatus,
    BranchTree,
    BudgetLedger,
    Replay,
    RollbackRequested,
    ToolReplay,
)
from orbit_audit.controller import AuditController
from orbit_audit.delivery import CommunicationWorld
from orbit_audit.policy import AuditPolicy
from orbit_audit.scope import AuditScope

PolicyFactory = Callable[[], AuditPolicy]
BRANCHES_KEY = "orbit_audit:branches"
BUDGET_KEY = "orbit_audit:budget"

_branch_controller: ContextVar[AuditController | None] = ContextVar(
    "orbit_audit_branch_controller", default=None
)


def _controller_for_branch() -> AuditController:
    controller = _branch_controller.get()
    if controller is None:
        raise RuntimeError("orbit_audit: no branch controller installed for this run")
    return controller


@dataclass
class BranchOutcome:
    status: BranchStatus
    reason: str
    state: TaskState
    store: dict[str, Any]
    error: BaseException | None = None
    messages: tuple[dict[str, Any], ...] = ()


@solver
def audit_solver(
    policy: PolicyFactory,
    *,
    tool_bindings: Mapping[str, Any] | None = None,
    tool_replay: Mapping[str, ToolReplay | str] | None = None,
    limits: AuditLimits | None = None,
    on_controller: Callable[[AuditController], None] | None = None,
    scope: AuditScope | None = None,
) -> Solver:
    """Audit each sample under a fresh ``policy()``, forking on rollback."""
    inner = mas_orchestrator(
        controller=_controller_for_branch,
        **({"tool_bindings": tool_bindings} if tool_bindings is not None else {}),
    )
    replay_policies = {name: ToolReplay(value) for name, value in (tool_replay or {}).items()}

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        auditor = policy()
        budget = BudgetLedger(limits or AuditLimits())
        tree = BranchTree()
        initial = _fresh_state(state)
        pending: list[Replay | None] = [None]
        completed: BranchOutcome | None = None
        outcome: BranchOutcome | None = None
        while pending:
            replay = pending.pop(0)
            node = tree.add(
                parent=replay.parent if replay else None,
                fork_revision=replay.fork_revision if replay else None,
                fork_reason=replay.reason if replay else "",
                interventions=replay.interventions if replay else (),
            )
            budget.branches += 1
            ids = BranchIds()
            controller = AuditController(
                auditor, branch=node.id, branch_index=node.index, replay=replay,
                budget=budget, ids=ids, tool_replay=replay_policies, scope=scope,
            )
            controller.sample_id, controller.epoch = state.sample_id, state.epoch
            if on_controller is not None:
                on_controller(controller)
            outcome = await _run_branch(initial, inner, generate, controller, ids, node.id)
            tree.finish(node.id, outcome.status, reason=outcome.reason,
                        revisions=controller.revision, output=_completion(outcome.state),
                        messages=outcome.messages)
            if outcome.status is BranchStatus.COMPLETED:
                completed = outcome
            if isinstance(outcome.error, RollbackRequested):
                rollback = outcome.error.rollback
                k = rollback.to_revision
                pending.append(Replay(parent=node.id, steps=tuple(controller.steps[:k]),
                                      fork_revision=k, interventions=rollback.interventions,
                                      reason=rollback.reason))
            elif outcome.status is BranchStatus.ERROR:
                _adopt(state, outcome, tree, budget)
                raise outcome.error  # infrastructure failures fail loudly
        _adopt(state, completed or outcome, tree, budget)
        return state

    return solve


async def _run_branch(
    initial: TaskState, inner: Solver, generate: Generate, controller: AuditController,
    ids: BranchIds, name: str,
) -> BranchOutcome:
    async def body() -> BranchOutcome:
        state = _fresh_state(initial)
        init_subtask_store(state.store)  # context-local to this task
        _branch_controller.set(controller)
        error: BaseException | None = None
        with id_scope(ids):
            async with span(name, type="branch"):
                try:
                    state = await inner(state, generate)
                except Exception as exc:  # classified below; unknown errors re-raised
                    error = exc
        ended = controller.ending or _branch_end(error)
        if ended is not None:
            status, reason = ended.status, str(ended)
        elif isinstance(error, LimitExceededError):
            status, reason = BranchStatus.BUDGET_EXHAUSTED, f"Inspect limit: {error}"
        elif error is not None:
            status, reason = BranchStatus.ERROR, f"{type(error).__name__}: {error}"
        else:
            status, reason = BranchStatus.COMPLETED, ""
        return BranchOutcome(status, reason, state, dict(store().items()), ended or error,
                             messages=CommunicationWorld.current().summary())

    loop = asyncio.get_running_loop()
    return await loop.create_task(body(), context=contextvars.copy_context())


def _branch_end(error: BaseException | None) -> BranchEnded | None:
    """The ``BranchEnded`` behind ``error``, through notes, causes and groups."""
    seen: set[int] = set()
    stack = [error]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BranchEnded):
            return current
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        stack.extend([current.__cause__, current.__context__])
    return None


def _fresh_state(base: TaskState) -> TaskState:
    return TaskState(
        model=base.model,
        sample_id=base.sample_id,
        epoch=base.epoch,
        input=copy.deepcopy(base.input),
        messages=copy.deepcopy(base.messages),
        target=base.target,
        metadata=copy.deepcopy(base.metadata),
        store=copy.deepcopy(dict(base.store.items())),
    )


def _completion(state: TaskState) -> str | None:
    return state.output.completion if state.output is not None else None


def _adopt(state: TaskState, outcome: BranchOutcome, tree: BranchTree,
           budget: BudgetLedger) -> None:
    """Make the sample's state that of ``outcome``'s branch, plus audit records."""
    for key in list(state.store.keys()):
        state.store.delete(key)
    for key, value in outcome.store.items():
        state.store.set(key, value)
    state.messages = outcome.state.messages
    state.output = outcome.state.output
    state.metadata.clear()
    state.metadata.update(outcome.state.metadata)
    state.store.set(BRANCHES_KEY, [node.model_dump(mode="json") for node in tree.nodes])
    state.store.set(BUDGET_KEY, budget.summary())
