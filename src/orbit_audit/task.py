"""Build audited Inspect tasks from Orbit configurations."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from inspect_ai import Task, task_with
from orbit import multi_agent_task

from orbit_audit.branches import AuditLimits, ToolReplay
from orbit_audit.controller import Approver, AuditController
from orbit_audit.runner import PolicyFactory, audit_solver
from orbit_audit.scope import AuditScope


def audit_task(
    *,
    policy: PolicyFactory,
    on_controller: Callable[[AuditController], None] | None = None,
    tool_replay: Mapping[str, ToolReplay | str] | None = None,
    limits: AuditLimits | None = None,
    scope: AuditScope | None = None,
    approver: Approver | None = None,
    **orbit_task: Any,
) -> Task:
    """An Orbit ``multi_agent_task`` whose samples are run under an audit policy.

    ``orbit_task`` takes the same arguments as ``orbit.multi_agent_task``
    (setup, dataset, communication, execution, scheduler, scorer,
    tool_bindings, Inspect task options), which validates them. ``policy`` is
    called once per sample; the policy instance persists across that sample's
    branches. ``tool_replay`` declares which bound tools are pure and may be
    re-executed when a rollback replays a branch (Orbit's channel tools and
    submit are pure by default). ``limits`` bounds branches and live model
    calls across the whole audit. ``scope`` enforces the audit's brief: the
    operations and agents its policy may use (an LLM auditor is offered only
    in-scope tools). ``approver``, if given, vets each valid live operation
    that puts content in front of agents; a reason it returns rejects that
    operation's batch, recorded and told to the policy like any rejection
    (``auditor.RealismCheck`` is one). ``on_controller`` receives each branch's
    controller, for tests and scripted analysis that need the ledger
    in-process; saved logs carry the same evidence as ``orbit_audit_*`` events.
    """
    base = multi_agent_task(**orbit_task)
    solver = audit_solver(
        policy, tool_bindings=orbit_task.get("tool_bindings"), tool_replay=tool_replay,
        limits=limits, on_controller=on_controller, scope=scope, approver=approver,
    )
    return task_with(base, solver=solver)
