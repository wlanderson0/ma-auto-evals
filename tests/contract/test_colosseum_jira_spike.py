"""Orbit's Jira task in Colosseum's three conditions runs unchanged under a passive audit.

Runs ``experiments/colosseum/jira_audit_spike.py`` on its scripted agents: each condition plain and
wrapped in ``audit_solver``, with the Jira, DCOP and collusion scores compared.
"""

from __future__ import annotations

import pytest

from experiments.colosseum.jira_audit_spike import COALITION, CONDITIONS, DEVELOPERS, compare


@pytest.mark.parametrize("condition", list(CONDITIONS))
def test_passive_audit_leaves_jira_scores_unchanged(condition: str) -> None:
    result = compare(condition)
    plain, audited = result["plain"], result["audited"]

    assert plain["status"] == audited["status"] == "success"
    assert result["mismatched"] == []
    assert audited["branch_events"] == [("started", None), ("finished", "completed")]
    assert plain["branch_events"] == []
    # One claim each (max_claims_per_agent=1), and every developer claimed.
    assert sorted(audited["assignments"].values()) == [f"dev_{i}" for i in range(DEVELOPERS)]


def test_only_the_coalition_uses_the_secret_channel() -> None:
    emergent = compare("emergent")["audited"]
    secret = {s for s, channel in emergent["channel_posts"] if channel.startswith("secret:")}
    assert secret == set(COALITION)
    assert "collusion_scorer" in emergent["scores"]


def test_only_prompted_shows_the_coalition_prompt() -> None:
    assert compare("emergent")["audited"]["coalition_prompt_seen"] == []
    assert compare("prompted")["audited"]["coalition_prompt_seen"] == sorted(COALITION)
