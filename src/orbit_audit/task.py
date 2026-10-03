"""Build audited Inspect tasks from Orbit configurations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from inspect_ai import Task, task_with
from orbit import multi_agent_task
from orbit.solvers.orchestrator import mas_orchestrator

from orbit_audit.controller import AuditController
from orbit_audit.policy import AuditPolicy

PolicyFactory = Callable[[], AuditPolicy]


def audit_task(
    *,
    policy: PolicyFactory,
    on_controller: Callable[[AuditController], None] | None = None,
    **orbit_task: Any,
) -> Task:
    """An Orbit ``multi_agent_task`` whose samples are run under an audit policy.

    ``orbit_task`` takes the same arguments as ``orbit.multi_agent_task``
    (setup, dataset, communication, execution, scheduler, scorer,
    tool_bindings, Inspect task options), which validates them. ``policy`` is
    called once per sample. ``on_controller`` receives each sample's controller,
    for tests and scripted analysis that need the ledger in-process; saved logs
    carry the same evidence as ``orbit_audit_intervention`` events.
    """

    def controller() -> AuditController:
        instance = AuditController(policy())
        if on_controller is not None:
            on_controller(instance)
        return instance

    base = multi_agent_task(**orbit_task)
    bindings = orbit_task.get("tool_bindings")
    solver = mas_orchestrator(
        controller=controller, **({"tool_bindings": bindings} if bindings is not None else {}),
    )
    return task_with(base, solver=solver)
