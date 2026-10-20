"""The pure rule that finds each agent's final model request (issue #19, stage 1)."""

from __future__ import annotations

from orbit_audit.measurements import (
    Alignment,
    Gen,
    ModelCall,
    align_requests,
    stored_requests,
)


def live(agent, index):
    return Gen(agent, index, exposed=True, replayed=False)


def replayed(agent, index):
    return Gen(agent, index, exposed=True, replayed=True)


def written(agent, index):
    return Gen(agent, index, exposed=False, replayed=False)


def test_live_generations_map_to_their_events_in_order():
    result = align_requests(
        branch="branch-0", parent=None,
        generations=[live("bob", 0), live("bob", 1), live("bob", 2)],
        calls=[ModelCall("e0", "bob", "branch-0"), ModelCall("e1", "bob", "branch-0"),
               ModelCall("e2", "bob", "branch-0")])
    assert result.lists["bob"] == [("e0", "branch-0"), ("e1", "branch-0"), ("e2", "branch-0")]
    assert stored_requests(result)["bob"] == {"event": "e2", "via": "branch-0", "why": ""}


def test_other_agents_and_other_branches_are_not_matched():
    result = align_requests(
        branch="branch-1", parent=None, generations=[live("bob", 0)],
        calls=[ModelCall("x", "carol", "branch-1"), ModelCall("y", "bob", "branch-0"),
               ModelCall("z", "bob", "branch-1")])
    assert result.lists["bob"] == [("z", "branch-1")]


def test_a_replayed_prefix_is_taken_from_the_parent_at_the_same_index():
    parent = {"bob": [("p0", "branch-0"), ("p1", "branch-0"), ("p2", "branch-0")]}
    result = align_requests(
        branch="branch-1", parent=parent,
        generations=[replayed("bob", 0), live("bob", 1), live("bob", 2)],
        calls=[ModelCall("c1", "bob", "branch-1"), ModelCall("c2", "bob", "branch-1")])
    assert result.lists["bob"] == [("p0", "branch-0"), ("c1", "branch-1"), ("c2", "branch-1")]


def test_an_agent_replayed_in_full_points_at_its_own_last_generation_not_the_parents_last():
    parent = {"bob": [("p0", "branch-0"), ("p1", "branch-0"), ("p2", "branch-0")]}
    result = align_requests(
        branch="branch-1", parent=parent,
        generations=[replayed("bob", 0), replayed("bob", 1)], calls=[])
    assert stored_requests(result)["bob"] == {"event": "p1", "via": "branch-0", "why": ""}


def test_a_grandparent_event_keeps_its_owning_branch():
    parent = {"bob": [("g0", "branch-0")]}
    result = align_requests(branch="branch-2", parent=parent,
                            generations=[replayed("bob", 0)], calls=[])
    assert stored_requests(result)["bob"]["via"] == "branch-0"


def test_auditor_written_generations_in_the_middle_do_not_shift_later_events():
    result = align_requests(
        branch="branch-0", parent=None,
        generations=[live("alice", 0), written("alice", 1), live("alice", 2)],
        calls=[ModelCall("a0", "alice", "branch-0"), ModelCall("a2", "alice", "branch-0")])
    assert result.lists["alice"] == [("a0", "branch-0"), None, ("a2", "branch-0")]


def test_a_last_generation_the_auditor_wrote_has_no_event_and_says_why():
    result = align_requests(
        branch="branch-0", parent=None, generations=[live("alice", 0), written("alice", 1)],
        calls=[ModelCall("a0", "alice", "branch-0")])
    stored = stored_requests(result)["alice"]
    assert stored["event"] is None and stored["via"] is None and stored["why"]


def test_a_count_mismatch_is_reported_not_guessed():
    result = align_requests(
        branch="branch-0", parent=None, generations=[live("bob", 0), live("bob", 1)],
        calls=[ModelCall("only", "bob", "branch-0")])
    assert result.lists["bob"] == [None, None]
    stored = stored_requests(result)["bob"]
    assert stored["event"] is None and "2 live generation" in stored["why"]


def test_a_replayed_generation_with_no_parent_record_is_unknown():
    result = align_requests(branch="branch-1", parent=None,
                            generations=[replayed("bob", 0)], calls=[])
    assert result.lists["bob"] == [None]
    assert isinstance(result, Alignment)
