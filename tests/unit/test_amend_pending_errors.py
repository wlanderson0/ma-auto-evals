"""An amendment the schedule refuses is a rejected op, not a failed branch (G3).

Orbit raises ``RuntimeError`` for an amendment at a non-quiescent boundary or on a
terminated schedule (``ScheduleRuntime._check_amendment``), before changing anything. The
controller catches ``OpError`` and ``ValueError`` only, so ``AmendPending`` turns it into
an ``OpError``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from orbit.configs.scheduling import ScheduleConfig
from orbit.execution.plans import ScheduleRuntime

from orbit_audit import delivery as delivery_module
from orbit_audit.controller import AuditController
from orbit_audit.delivery import CommunicationWorld
from orbit_audit.ops import AmendPending, OpError
from orbit_audit.policy import ScriptedPolicy


def terminated_schedule() -> ScheduleRuntime:
    schedule = ScheduleRuntime(ScheduleConfig(steps=[["pm"], ["coder"]]), ["pm", "coder"])
    schedule.begin_step()
    schedule.finish_step("cancelled")
    return schedule


def test_a_refused_amendment_is_an_op_error():
    schedule = terminated_schedule()
    pending = list(schedule.pending_steps)
    amend = AmendPending(steps=(("pm",),), schedule_revision=schedule.revision)
    with pytest.raises(OpError, match="refused by the schedule"):
        amend.apply(SimpleNamespace(schedule=schedule), actor="test")
    assert list(schedule.pending_steps) == pending


def test_the_controller_records_it_as_rejected(monkeypatch):
    schedule = terminated_schedule()
    amend = AmendPending(steps=(("pm",),), schedule_revision=schedule.revision)
    controller = AuditController(ScriptedPolicy(lambda view: amend))
    controller.runtime = SimpleNamespace(
        schedule=schedule,
        config=SimpleNamespace(setup=SimpleNamespace(agents=[SimpleNamespace(name="pm"),
                                                             SimpleNamespace(name="coder")])))
    world = CommunicationWorld(messages={}, channels={}, permitted_edges=None)
    monkeypatch.setattr(delivery_module.CommunicationWorld, "current",
                        classmethod(lambda cls: world))
    asyncio.run(controller.after_turn(1, []))
    assert controller.ledger.applied() == ()
    [record] = controller.ledger.rejected()
    assert "refused by the schedule" in record.reason
