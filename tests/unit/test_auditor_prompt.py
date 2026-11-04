"""What the LLM auditor is told about the agents it may touch."""

from __future__ import annotations

from orbit_audit.auditor.prompts import AUDITOR_SYSTEM


def test_the_auditor_is_told_that_touching_an_agent_costs_evidence():
    text = " ".join(AUDITOR_SYSTEM.split())
    assert "clean" in text and "no longer evidence" in text


def test_the_agents_are_not_called_targets_since_any_may_be_touched():
    assert "target agents" not in AUDITOR_SYSTEM
