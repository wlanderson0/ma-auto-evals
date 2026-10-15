"""Concurrent schedule steps are refused until audits support them.

A pause assumes one agent is mid-turn at a time, and rollback replay matches
recorded outputs in arrival order. Parallel turns break both, so a plan with a
``concurrent`` step fails up front instead of producing unsound evidence.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from orbit import ExecutionConfig, ScheduleConfig

from orbit_audit.controller import AuditController
from orbit_audit.policy import ScriptedPolicy
from orbit_audit.task import audit_task
from tests.support.scenarios import release_team

PARALLEL = ScheduleConfig(steps=[
    {"id": "ask", "agents": ["pm"]},
    {"id": "work", "agents": ["coder", "reviewer"], "concurrent": True},
])


def test_audit_tasks_refuse_plans_with_concurrent_steps():
    execution = ExecutionConfig(quantum="model_step", output_agent="reviewer", plan=PARALLEL)
    with pytest.raises(ValueError, match=r"concurrent schedule steps yet \(steps \['work'\]\)"):
        audit_task(policy=lambda: ScriptedPolicy(lambda view: None),
                   **release_team(execution=execution))


def test_the_controller_refuses_a_concurrent_plan_it_is_started_with():
    # Covers plans that reach the runtime without passing audit_task's check.
    controller = AuditController(ScriptedPolicy(lambda view: None))
    runtime = SimpleNamespace(schedule=SimpleNamespace(config=PARALLEL))
    with pytest.raises(ValueError, match="concurrent schedule steps"):
        asyncio.run(controller.start(runtime))
    assert controller.runtime is None


def test_sequential_plans_are_still_accepted():
    audit_task(policy=lambda: ScriptedPolicy(lambda view: None), **release_team())
