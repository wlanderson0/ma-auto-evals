"""Validity checks: rejections and warnings for built variants (L2 Task 4; spec §3.4)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from experiments.l2.apply import apply
from experiments.l2.brief import parse_brief
from experiments.l2.model import InnerSpec, Mutation, Problems, Variant
from experiments.l2.validity import check, reachable
from experiments.scenarios import SCENARIOS, build_team
from orbit_audit.scope import AuditScope

MILESTONE1 = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / "milestone1.md"
GRID = parse_brief(MILESTONE1).variants
OPENING = (("town", "Morning all."),)
WIDE = AuditScope(ops={"replace_generation", "amend_pending"}, agents=None)
ADVOCATE = Mutation("roster", "add_agent", {
    "name": "erin", "persona": "Erin, an engineer", "model": "mockllm/model",
    "channels": {"read": ["town"], "write": ["town"]}, "turns": False})


def activate(at: str = "after_round:1", agent: str = "erin") -> Mutation:
    return Mutation("schedule", "activate_agent", {"agent": agent, "position": "after:alice"},
                    at=at)


def problems(*mutations: Mutation, condition: str = "scripted", opening=OPENING,
             scope: AuditScope | None = None) -> Problems:
    base = SCENARIOS["mfa_rumor"]({})
    variant = Variant(id="X", base="mfa_rumor", mutations=mutations, rationale="r",
                      inner=InnerSpec(condition=condition, opening=opening, scope=scope))
    return check(base, apply(base, variant), variant)


def topo(op: str, agent: str, channel: str) -> Mutation:
    return Mutation("topology", op, {"agent": agent, "channel": channel})


@pytest.mark.parametrize("variant", GRID, ids=lambda v: v.id)
def test_every_milestone1_variant_is_clean(variant):
    base = SCENARIOS[variant.base]({})
    assert check(base, apply(base, variant), variant) == Problems()


def test_plain_scripted_variant_is_clean():
    assert problems() == Problems()


# ---------------------------------------------------------------- rejections
def test_a_build_error_is_a_rejection():
    base = SCENARIOS["mfa_rumor"]({})
    variant = Variant(id="X", base="mfa_rumor", mutations=(), inner=InnerSpec("scripted"),
                      rationale="r")
    found = check(base, None, variant, build_error="UnknownOp: unknown op topology/x")
    assert found.rejected and "does not build: UnknownOp" in found.rejections[0]


def test_a_channel_naming_an_unknown_agent_is_rejected():
    found = problems(topo("add_reader", "zed", "town"))
    assert any("['zed']" in r and "not in the setup" in r for r in found.rejections)


def test_the_output_agent_left_out_of_the_schedule_is_rejected():
    base = SCENARIOS["mfa_rumor"]({})
    variant = Variant(id="X", base="mfa_rumor", mutations=(), inner=InnerSpec("scripted"),
                      rationale="r")
    built = apply(base, variant)
    team = dataclasses.replace(built.team, rounds=None,
                               schedule=(("alice",), ("bob",), ("carol",)))
    built = dataclasses.replace(built, team=team, task=build_team(built.name, team))
    found = check(base, built, variant)
    assert any("output agent 'dave'" in r for r in found.rejections)


def test_an_opening_on_a_channel_the_seat_cannot_write_is_rejected():
    found = problems(opening=(("pod", "hi"),))
    assert any("posts on 'pod'" in r for r in found.rejections)
    found = problems(topo("remove_writer", "alice", "town"))
    assert any("posts on 'town'" in r for r in found.rejections)
    assert problems(topo("add_writer", "alice", "pod"), topo("add_reader", "alice", "pod"),
                    opening=(("pod", "hi"),)) == Problems()


def test_an_opening_outside_the_scripted_condition_is_rejected():
    found = problems(condition="baseline")
    assert any("only played by the scripted condition" in r for r in found.rejections)


@pytest.mark.parametrize("condition", ["l0", "l1"])
def test_mid_run_changes_with_an_inner_auditor_are_rejected(condition):
    found = problems(ADVOCATE, activate(), condition=condition, opening=())
    assert any("combined policy" in r for r in found.rejections)


def test_mid_run_changes_without_a_helper_are_rejected():
    found = problems(ADVOCATE, activate(), condition="baseline", opening=())
    assert any("need the scripted helper" in r for r in found.rejections)


@pytest.mark.parametrize("mutations, message", [
    ((activate(),), "'erin' is not in the setup"),
    ((ADVOCATE, activate(agent="bob")), "'bob' is already in the schedule"),
    ((ADVOCATE, activate("after_round:0")), "never takes effect"),
    ((ADVOCATE, Mutation("schedule", "activate_agent", {"agent": "erin",
                                                        "position": "after:zed"},
                         at="after_round:1")), "names 'zed', who is not in the schedule"),
    ((ADVOCATE, activate("after_round:3")), "never takes effect"),
    ((ADVOCATE, Mutation("schedule", "set_rounds", {"rounds": 2}), activate("after_round:2")),
     "never takes effect"),
])
def test_an_activation_that_can_never_take_effect_is_rejected(mutations, message):
    assert any(message in r for r in problems(*mutations, scope=WIDE).rejections)


def test_the_last_useful_activation_round_is_accepted():
    assert problems(ADVOCATE, activate("after_round:2"), scope=WIDE) == Problems()


def test_an_activation_needs_a_scope_with_amend_pending_for_everyone():
    found = problems(ADVOCATE, activate())  # the seat-only default scope
    assert found.rejections == (found.rejections[0],)
    assert "must allow amend_pending with agents: null" in found.rejections[0]
    narrow = AuditScope(ops={"replace_generation", "amend_pending"}, agents={"alice"})
    assert problems(ADVOCATE, activate(), scope=narrow).rejected


def test_more_than_one_activation_is_rejected():
    found = problems(ADVOCATE, activate(), activate("after_round:2"), scope=WIDE)
    assert any("one activation per variant, not 2" in r for r in found.rejections)
    other = Mutation("roster", "add_agent", {**ADVOCATE.args, "name": "finn"})
    found = problems(ADVOCATE, other, activate(), activate(agent="finn"), scope=WIDE)
    assert any("one activation per variant, not 2" in r for r in found.rejections)


@pytest.mark.parametrize("condition", ["l0", "l1"])
def test_an_inner_auditor_on_a_changed_setup_is_rejected(condition):
    found = problems(topo("add_writer", "dave", "town"), condition=condition, opening=())
    assert any("goals generated from the variant's setup" in r for r in found.rejections)
    assert problems(condition=condition, opening=()) == Problems()
    persona = Mutation("persona", "append_line", {"agent": "bob", "line": "Be terse."})
    assert problems(persona, condition=condition, opening=()) == Problems()  # goal still true


@pytest.mark.parametrize("scope", [
    AuditScope(ops={"amend_pending"}, agents=None),
    AuditScope(ops={"replace_generation"}, agents={"bob"}),
])
def test_an_opening_the_scope_would_refuse_is_rejected(scope):
    found = problems(scope=scope)
    assert any("would refuse the opening" in r for r in found.rejections)


def test_a_one_round_variant_cannot_activate_anyone():
    found = problems(ADVOCATE, Mutation("schedule", "set_rounds", {"rounds": 1}), activate(),
                     scope=WIDE)
    assert "activate_agent cannot take effect: the variant has only 1 round" in (
        found.rejections)


def test_repeated_problems_are_listed_once():
    found = problems(activate(agent="zed"), activate("after_round:2", agent="zed"), scope=WIDE)
    assert found.rejections.count("activate_agent: 'zed' is not in the setup") == 1
    assert any("one activation per variant" in r for r in found.rejections)


def test_a_malformed_activation_agent_is_a_build_error():
    from experiments.l2.apply import ApplyError

    with pytest.raises(ApplyError, match="agent must be non-empty text"):
        problems(ADVOCATE, Mutation("schedule", "activate_agent",
                                    {"agent": ["erin"], "position": "after:alice"},
                                    at="after_round:1"), scope=WIDE)


# ------------------------------------------------------------------ warnings
def test_a_scheduled_agent_that_can_do_nothing_is_warned():
    found = problems(topo("remove_reader", "dave", "pod"), topo("remove_writer", "dave", "pod"))
    assert not found.rejected
    assert any("'dave' is scheduled but can read and write nothing" in w
               for w in found.warnings)


def test_an_activated_agent_that_can_do_nothing_is_warned():
    silent = Mutation("roster", "add_agent", {**ADVOCATE.args, "channels": {}})
    found = problems(silent, activate(), scope=WIDE)
    assert not found.rejected
    assert "'erin' is scheduled but can read and write nothing" in found.warnings


def test_the_seat_never_scheduled_is_warned():
    base = SCENARIOS["mfa_rumor"]({})
    variant = Variant(id="X", base="mfa_rumor", mutations=(), inner=InnerSpec("scripted"),
                      rationale="r")
    built = apply(base, variant)
    team = dataclasses.replace(built.team, rounds=None,
                               schedule=(("bob",), ("carol",), ("dave",)))
    built = dataclasses.replace(built, team=team, task=build_team(built.name, team))
    found = check(base, built, variant)
    assert "the seat 'alice' is never scheduled" in found.warnings


def test_nothing_built_and_no_reason_is_a_rejection():
    base = SCENARIOS["mfa_rumor"]({})
    variant = Variant(id="X", base="mfa_rumor", mutations=(), inner=InnerSpec("scripted"),
                      rationale="r")
    assert check(base, None, variant).rejections == ("the variant was not built",)


def test_a_target_never_scheduled_is_warned():
    base = SCENARIOS["mfa_rumor"]({})
    variant = Variant(id="X", base="mfa_rumor", mutations=(), inner=InnerSpec("scripted"),
                      rationale="r")
    built = apply(base, variant)
    team = dataclasses.replace(built.team, rounds=None,
                               schedule=(("alice",), ("bob",), ("dave",)))
    built = dataclasses.replace(built, team=team, task=build_team(built.name, team))
    found = check(base, built, variant)
    assert found.warnings == ("the target 'carol' is never scheduled",)


def test_an_unreachable_target_is_warned():
    found = problems(topo("remove_reader", "dave", "pod"))
    assert "the target 'dave' cannot be reached from the seat 'alice'" in found.warnings


def test_an_idle_agent_does_not_relay():
    """An Advocate that never acts, reading #town and writing #pod, is no bridge."""
    bridge = Mutation("roster", "add_agent", {
        **ADVOCATE.args, "channels": {"read": ["town"], "write": ["pod"]}})
    found = problems(bridge, topo("remove_writer", "bob", "pod"),
                     topo("remove_writer", "carol", "pod"), scope=WIDE)
    assert "the target 'dave' cannot be reached from the seat 'alice'" in found.warnings
    channels = apply(SCENARIOS["mfa_rumor"]({}), Variant(
        id="X", base="mfa_rumor", mutations=(bridge,), inner=InnerSpec("scripted"),
        rationale="r")).task["communication"].channels
    assert "dave" in reachable("alice", list(channels))  # anyone relays when not limited
    assert "dave" in reachable("alice", list(channels), relays={"bob", "erin"})


def test_reachability_follows_relays():
    channels = SCENARIOS["mfa_rumor"]({}).task["communication"].channels
    assert reachable("alice", list(channels)) == {"bob", "carol", "dave"}
    assert reachable("dave", list(channels)) == {"alice", "bob", "carol"}  # relayed to town
