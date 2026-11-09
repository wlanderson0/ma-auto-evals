"""Differential tests: the Lean model (``lean/``) against the Python it mirrors.

Each test generates a world, a scope, a boundary and an op, asks the Lean model
(through ``tests/lean/driver.py``) and the real ``orbit_audit`` code, and compares:

* ``AuditScope.problem`` verdicts and ``op.affected`` sets (every op);
* ``AuditController._problem`` verdicts (every op but rollback);
* one boundary's batch through ``AuditController._decide`` and its rollback path: whether
  it rolls back, which message ops are applied, which family ops reach the boundary;
* for the message ops, whether ``apply`` succeeds and which agents' views change, with
  the Python views computed through ``DeliveryOverlay.deliver``; for ``halt`` and
  ``amend_pending``, the same against Orbit's ``ScheduleRuntime``;
* for every other op, that the agents Lean's effect changes are within the Python's
  ``affected`` or a documented exception (G1, G2, D1).

Not compared yet: the effects of generation, activation and tool ops against the Python
(their effect is in the controller's boundary methods, with Inspect objects), absolute
inboxes through Orbit's own ``read_messages``, and whole episodes with replay and forks.

Skipped unless the ``lean`` binary (``LEAN``) and a built model are present (an error
instead when ``LEAN_REQUIRED`` is set, as in CI). Hypothesis
profiles: ``lean-pr`` (default, fixed seed) and ``lean-nightly`` (``HYPOTHESIS_PROFILE``).
"""

from __future__ import annotations

import asyncio
import copy
import os
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import event as _hypothesis_event
from hypothesis import strategies as st
from hypothesis.control import currently_in_test_context
from orbit.configs.scheduling import ScheduleConfig
from orbit.execution.control import DeliveryRequest
from orbit.execution.plans import ScheduleRuntime
from pydantic import ValidationError

from orbit_audit import delivery as delivery_module
from orbit_audit.boundaries import Boundary
from orbit_audit.branches import RollbackRequested
from orbit_audit.controller import AuditController
from orbit_audit.ops import (
    ActivationOp,
    GenerationOp,
    OpError,
    ToolOp,
    TurnOp,
    op_types,
    parse_op,
)
from orbit_audit.policy import BoundaryView, ScriptedPolicy
from orbit_audit.scope import AuditScope
from tests.lean.driver import python_overlay, python_world

settings.register_profile("lean-pr", max_examples=1000, derandomize=True, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("lean-nightly", max_examples=5000, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
PROFILE = os.environ.get("HYPOTHESIS_PROFILE", "lean-pr")

pytestmark = pytest.mark.timeout(600)


def event(label: str) -> None:
    """Hypothesis statistics, when called from inside a Hypothesis test."""
    if currently_in_test_context():
        _hypothesis_event(label)


# ------------------------------------------------------------------ generators
AGENTS = ["alice", "bob", "carol", "dave"]
NAMES = [*AGENTS, "", "zed"]  # ill-formed: empty and unknown
CHANNELS = ["town", "pod"]
OP_NAMES = sorted(op_types())


@st.composite
def worlds(draw) -> dict[str, Any]:
    agents = draw(st.lists(st.sampled_from(AGENTS), unique=True, max_size=4))
    channels = [{"id": c, "readers": draw(st.lists(st.sampled_from(AGENTS), unique=True)),
                 "writers": draw(st.lists(st.sampled_from(AGENTS), unique=True))}
                for c in CHANNELS[:draw(st.integers(1, 2))]]
    routes = draw(st.none() | st.lists(st.tuples(st.sampled_from(AGENTS), st.sampled_from(AGENTS))
                                       .map(list), max_size=6))
    messages = []
    for i in range(draw(st.integers(0, 4))):
        channel = draw(st.sampled_from(channels))
        sender = draw(st.sampled_from(AGENTS))
        recipients = draw(st.lists(st.sampled_from([a for a in AGENTS if a != sender]), unique=True))
        messages.append({"id": f"m{i}", "channel": channel["id"], "sender": sender,
                         "recipients": recipients, "content": f"text {i}"})
    ids = [m["id"] for m in messages] or ["m0"]
    key = st.tuples(st.sampled_from(ids), st.none() | st.sampled_from(AGENTS)).map(list)
    injections = [{"id": f"pre-{i}", "channel": draw(st.sampled_from(CHANNELS)),
                   "sender": draw(st.sampled_from(AGENTS)),
                   "recipients": draw(st.lists(st.sampled_from(AGENTS), unique=True, min_size=1)),
                   "content": "earlier injection"} for i in range(draw(st.integers(0, 2)))]
    overlay = {
        "rewrites": draw(st.lists(st.tuples(key, st.sampled_from(["r1", "r2"])).map(list),
                                  max_size=3)),
        "drops": draw(st.lists(key, max_size=2)),
        "injections": injections,
        "delivered": draw(st.lists(st.tuples(st.sampled_from(AGENTS), st.sampled_from(ids))
                                   .map(list), max_size=3)),
    }
    schedule = draw(st.none() | st.builds(
        lambda rev, pending: {"revision": rev, "pending": pending}, st.integers(0, 2),
        st.lists(st.lists(st.sampled_from(AGENTS), unique=True, min_size=1), max_size=3)))
    return {"agents": agents, "channels": channels, "routes": routes, "messages": messages,
            "overlay": overlay, "schedule": schedule, "halted": draw(st.booleans())}


def ops(world: dict[str, Any], depth: int = 0,
        only: tuple[str, ...] | None = None) -> st.SearchStrategy[dict[str, Any]]:
    """Ops as the Lean driver reads them (Python field names; tool calls as names)."""
    name = st.sampled_from(NAMES)
    ids = [m["id"] for m in world["messages"]] + ["missing"]
    recipients = st.none() | st.lists(name, unique=True, max_size=3)
    call_id = st.none() | st.sampled_from(["call-1", "call-2"])
    builders = {
        "replace_generation": st.builds(lambda a, c, t: {"op": "replace_generation", "agent": a, "content": c,
                                   "tool_calls": t},
                  name, st.sampled_from(["", "x"]), st.lists(st.just("channel_post"), max_size=1)),
        "edit_generation": st.builds(lambda a: {"op": "edit_generation", "agent": a, "replacements": [["a", "b"]]},
                  name),
        "force_tool_call": st.builds(lambda a: {"op": "force_tool_call", "agent": a, "calls": ["channel_post"]}, name),
        "stage_input": st.builds(lambda a: {"op": "stage_input", "agent": a, "content": "note"}, name),
        "block_tool_call": st.builds(lambda a, t: {"op": "block_tool_call", "agent": a, "tool_call_id": t,
                                "message": "no"}, name, call_id),
        "simulate_tool_result": st.builds(lambda a, t: {"op": "simulate_tool_result", "agent": a, "tool_call_id": t,
                                "result": "ok"}, name, call_id),
        "override_tool_result": st.builds(lambda a, t: {"op": "override_tool_result", "agent": a, "tool_call_id": t,
                                "result": "ok"}, name, call_id),
        "rewrite_message": st.builds(lambda i, r: {"op": "rewrite_message", "message_id": i, "recipients": r,
                                "content": "forged"}, st.sampled_from(ids), recipients),
        "drop_message": st.builds(lambda i, r: {"op": "drop_message", "message_id": i, "recipients": r},
                  st.sampled_from(ids), recipients),
        "inject_message": st.builds(lambda c, s, r, o: {"op": "inject_message", "channel_id": c, "sender": s,
                                      "recipients": r, "content": "planted",
                                      "override_topology": o},
                  st.sampled_from([*CHANNELS, "nochan"]), name,
                  st.lists(name, unique=True, min_size=1, max_size=3), st.booleans()),
        "halt": st.just({"op": "halt"}),
        "amend_pending": st.builds(lambda steps, rev: {"op": "amend_pending", "steps": steps,
                                      "schedule_revision": rev},
                  st.lists(st.lists(st.sampled_from(world["agents"] or NAMES) | name, max_size=3),
                           min_size=1, max_size=3),
                  st.sampled_from([(world["schedule"] or {"revision": 0})["revision"], 0, 1, 2])),
    }
    if depth == 0:
        builders["rollback"] = st.builds(
            lambda t, ivs: {"op": "rollback", "to_revision": t, "interventions": ivs},
            st.integers(1, 6), st.lists(st.deferred(lambda: ops(world, 1)), max_size=2))
    return st.one_of([b for n, b in builders.items() if only is None or n in only])


def scopes() -> st.SearchStrategy[dict[str, Any]]:
    return st.builds(lambda o, a: {"ops": o, "agents": a},
                     st.none() | st.lists(st.sampled_from(OP_NAMES), unique=True),
                     st.none() | st.lists(st.sampled_from(AGENTS), unique=True))


BOUNDARIES = {
    "replace_generation": ["generation"], "edit_generation": ["generation"],
    "force_tool_call": ["generation"], "stage_input": ["before_activation"],
    "block_tool_call": ["tool_call"], "simulate_tool_result": ["tool_call"],
    "override_tool_result": ["tool_result"], "halt": ["between_turns"],
    "amend_pending": ["between_turns"],
}


def aligned_ctxs(op: dict[str, Any]) -> st.SearchStrategy[dict[str, Any]]:
    """Boundaries where ``op`` can apply, at its own agent or another (G2 needs another)."""
    if op["op"] not in BOUNDARIES:
        return ctxs()
    boundary = st.sampled_from(BOUNDARIES[op["op"]])
    agent = (st.none() if op["op"] in ("halt", "amend_pending")
             else st.sampled_from([op["agent"]] if op.get("agent") else AGENTS)
             | st.sampled_from(AGENTS))
    call = st.just(op.get("tool_call_id")) | st.none() | st.just("call-1")
    return st.builds(lambda b, a, t: {"boundary": b, "revision": 5, "agent": a,
                                      "tool_call_id": t}, boundary, agent, call) | ctxs()


def ops_at(world: dict[str, Any], boundary: str) -> st.SearchStrategy[dict[str, Any]]:
    """Mostly ops that can apply at ``boundary``, plus rollbacks and strays."""
    fitting = tuple(n for n, bs in BOUNDARIES.items() if boundary in bs) + MESSAGE_OPS
    return ops(world, only=fitting) | ops(world, only=("rollback",)) | ops(world)


def ctxs() -> st.SearchStrategy[dict[str, Any]]:
    return st.builds(lambda b, a, t: {"boundary": b.value, "revision": 5, "agent": a,
                                      "tool_call_id": t},
                     st.sampled_from(list(Boundary)), st.none() | st.sampled_from(NAMES),
                     st.none() | st.sampled_from(["call-1", "call-2"]))


# --------------------------------------------------------------- the Python side
def python_op(op: dict[str, Any]):
    """The real op; ``None`` when pydantic refuses the payload (Python cannot build it)."""
    data = dict(op)
    if "tool_calls" in data:
        data["tool_calls"] = [{"function": f, "arguments": {}} for f in data["tool_calls"]]
    if "calls" in data:
        data["calls"] = [{"function": f, "arguments": {}} for f in data["calls"]]
    if "replacements" in data:
        data["replacements"] = [{"old": o, "new": n} for o, n in data["replacements"]]
    if "interventions" in data:
        data["interventions"] = [_python_data(iv) for iv in data["interventions"]]
    try:
        return parse_op(data)
    except (ValidationError, OpError, ValueError):
        return None


def _python_data(op: dict[str, Any]) -> dict[str, Any]:
    built = python_op(op)
    if built is None:
        raise ValueError("invalid intervention")
    return {"op": built.name, **built.model_dump(mode="json", exclude={"expected_revision",
                                                                        "reason"})}


def scope_kind(reason: str | None) -> str | None:
    if reason is None:
        return None
    if reason.startswith("rollback intervention:"):
        return "intervention"
    if " affects " in reason:
        return "agents_outside"
    return "op_outside"


def problem_kind(reason: str | None) -> str | None:
    if reason is None:
        return None
    if "outside this audit's scope" in reason:
        return "scope:" + scope_kind(reason)
    for needle, kind in [("is not allowed at boundary", "boundary"),
                         ("unknown agents", "unknown_agents"),
                         ("but this boundary is", "targeted"), ("is for tool call", "pinned")]:
        if needle in reason:
            return kind
    raise AssertionError(f"unclassified reason {reason!r}")


FAMILY = {Boundary.GENERATION: GenerationOp, Boundary.BEFORE_ACTIVATION: ActivationOp,
          Boundary.TOOL_CALL: ToolOp, Boundary.TOOL_RESULT: ToolOp,
          Boundary.BETWEEN_TURNS: TurnOp}


def controller_problem(scope, ctx, op, world) -> str | None:
    controller = AuditController(ScriptedPolicy(), scope=scope)
    controller.runtime = SimpleNamespace(config=SimpleNamespace(setup=SimpleNamespace(
        agents=[SimpleNamespace(name=a) for a in world["agents"]])))
    pyworld = python_world(world)
    boundary = Boundary(ctx["boundary"])
    view = BoundaryView(boundary=boundary, revision=ctx["revision"], turn=None,
                        agent=ctx["agent"], tool_call_id=ctx["tool_call_id"])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(delivery_module.CommunicationWorld, "current",
                      classmethod(lambda cls: pyworld))
        return controller._problem(view, op, FAMILY[boundary])


def python_views(world: dict[str, Any], overlay, candidates: list[str]) -> dict[str, Any]:
    """Each candidate's inbox (as a sorted multiset) and words attributed to it."""
    pyworld = python_world(world)
    canonical = sorted(pyworld.messages.values(), key=lambda m: m.sequence)
    views = {}
    for agent in candidates:
        inbox = []
        for channel in pyworld.channels.values():
            if agent not in pyworld.readers(channel):
                continue
            selected = tuple(m for m in canonical if m.channel_id == channel.id and (
                m.sender == agent or (agent in m.recipients
                                      and pyworld.route_allowed(m.sender, agent))))
            shown = copy.deepcopy(overlay).deliver(DeliveryRequest(
                recipient=agent, channel_id=channel.id, messages=selected))
            inbox += [(m.channel_id, m.sender, m.content, tuple(m.recipients),
                       m.id if m.id in pyworld.messages else "*") for m in shown]
        rewritten = {key: text for key, text in overlay.rewrites.items()
                     if key[0] in pyworld.messages and pyworld.messages[key[0]].sender == agent}
        injected = sorted((m.channel_id, tuple(m.recipients), m.content)
                          for m in overlay.injections if m.sender == agent)
        views[agent] = (sorted(inbox), rewritten, injected)
    return views


def candidates(world: dict[str, Any], op) -> list[str]:
    names = [*world["agents"], *op.agents()]
    for m in world["messages"]:
        names += [m["sender"], *m["recipients"]]
    return sorted(set(names))


# ------------------------------------------------------------------ comparisons
def compare_problem(lean, world, scope, op) -> list[str]:
    """Differences between Lean and Python on ``problem`` and ``affected``."""
    py = python_op(op)
    pyworld = python_world(world)
    reply = lean.ask({"cmd": "problem", "world": world, "scope": scope, "op": op})
    expected = scope_kind(AuditScope(**scope).problem(py, pyworld))
    event(f"problem: {expected}")
    diffs = []
    if reply["problem"] != expected:
        diffs.append(f"problem: lean {reply['problem']}, python {expected}")
    if set(reply["affected"]) != set(py.affected(pyworld)):
        diffs.append(f"affected: lean {reply['affected']}, python {py.affected(pyworld)}")
    return diffs


def compare_check(lean, world, scope, ctx, op) -> list[str]:
    py = python_op(op)
    reply = lean.ask({"cmd": "check", "world": world, "scope": scope, "ctx": ctx, "op": op})
    expected = problem_kind(controller_problem(
        None if scope is None else AuditScope(**scope), ctx, py, world))
    event(f"check: {expected}")
    return [] if reply["problem"] == expected else [
        f"check: lean {reply['problem']}, python {expected}"]


def compare_effects(lean, world, op) -> list[str]:
    py = python_op(op)
    pyworld = python_world(world)
    overlay = python_overlay(world)
    before = python_views(world, overlay, candidates(world, py))
    try:
        py.apply(overlay, pyworld)
        error = None
    except OpError as exc:
        error = str(exc)
    reply = lean.ask({"cmd": "apply", "world": world, "op": op, "ok": True,
                      "ctx": {"boundary": "generation", "revision": 5, "agent": None,
                              "tool_call_id": None}})
    event(f"{op['op']}: {'error' if error else 'applied'}")
    if (reply["error"] is None) != (error is None):
        return [f"apply: lean {reply['error']}, python {error}"]
    if error is not None:
        return []
    after = python_views(world, overlay, candidates(world, py))
    changed = {a for a in before if before[a] != after[a]}
    diffs = []
    if changed != set(reply["changed"]):
        diffs.append(f"changed: lean {reply['changed']}, python {sorted(changed)}")
    if not changed <= set(py.affected(pyworld)):  # property 2 on the Python itself
        diffs.append(f"python changed {sorted(changed)} outside affected {py.affected(pyworld)}")
    return diffs


MESSAGE_OPS = ("rewrite_message", "drop_message", "inject_message")


def python_decide(world, scope, boundaries, agent, tool_call_id, ops) -> dict[str, Any]:
    """One boundary through the real ``AuditController._decide`` (and its rollback path)."""
    built = [python_op(o) for o in ops]
    revision = len(boundaries)
    policy = ScriptedPolicy(lambda v: built if v.revision == revision else None)
    controller = AuditController(policy, scope=scope)
    controller.runtime = SimpleNamespace(config=SimpleNamespace(
        setup=SimpleNamespace(agents=[SimpleNamespace(name=a) for a in world["agents"]]),
        defenses=[]))
    controller.overlay = python_overlay(world)
    pyworld = python_world(world)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(delivery_module.CommunicationWorld, "current",
                      classmethod(lambda cls: pyworld))
        for b in boundaries[:-1]:
            controller._view(Boundary(b))
        last = Boundary(boundaries[-1])
        view = controller._view(last, agent=agent, tool_call_id=tool_call_id)
        try:
            family = asyncio.run(controller._decide(view, FAMILY[last]))
            rollback = False
        except RollbackRequested:
            family, rollback = [], True
    applied = [r.op for r in controller.ledger.applied()]
    return {"rollback": rollback,
            "messages": [] if rollback else [n for n in applied if n in MESSAGE_OPS],
            "family": [o.name for o in family], "applied": applied if rollback else []}


def compare_decide(lean, world, scope, boundaries, agent, tool_call_id, ops) -> list[str]:
    expected = python_decide(world, None if scope is None else AuditScope(**scope), boundaries,
                             agent, tool_call_id, ops)
    reply = lean.ask({"cmd": "decide", "world": world, "scope": scope,
                      "boundaries": boundaries, "agent": agent, "tool_call_id": tool_call_id,
                      "ops": ops})
    event("decide: " + ("rollback" if expected["rollback"] else
                        "accepted" if expected["family"] or expected["messages"] else "nothing"))
    return [] if reply == expected else [f"decide: lean {reply}, python {expected}"]


def known_leak(world: dict[str, Any], ctx: dict[str, Any], op: dict[str, Any]) -> set[str]:
    """Agents a documented exception may reach outside ``affected``, computed on the
    Python side (independently of Lean's ``gapLeak``): D1 halt reaches everyone; G1
    amend_pending reaches every agent in the pending steps it replaces; G2 an empty
    ``agent`` reaches the boundary agent."""
    if op["op"] == "halt":
        return {"*"}
    if op["op"] == "amend_pending":
        return {a for step in (world["schedule"] or {"pending": []})["pending"] for a in step}
    if op.get("agent") == "" and ctx["agent"] is not None:
        return {ctx["agent"]}
    return set()


def compare_frame(lean, world, ctx, op, ok) -> list[str]:
    """Lean's effect of an op that passes the controller's other checks, against the
    Python's ``affected``: every agent Lean changes is in ``affected`` or in a documented
    exception (property 2 on the Python's own ``affected``)."""
    py = python_op(op)
    if problem_kind(controller_problem(None, ctx, py, world)) is not None:
        return []
    reply = lean.ask({"cmd": "apply", "world": world, "op": op, "ok": ok, "ctx": ctx})
    if reply["error"] is not None:
        event(f"frame {op['op']}: lean refused")
        return []
    outside = set(reply["changed"]) - set(py.affected(python_world(world)))
    leak = known_leak(world, ctx, op)
    event(f"frame {op['op']}: {'gap reached' if outside else 'within affected'}")
    if outside and "*" not in leak and not outside <= leak:
        return [f"{op['op']} changed {sorted(outside)} outside affected and known gaps"]
    return []


def compare_turn_effects(lean, world, op) -> list[str]:
    """``halt`` and ``amend_pending`` against Orbit's real ``ScheduleRuntime``."""
    py = python_op(op)
    schedule = world["schedule"]
    if schedule is None or not all(a in world["agents"] for s in schedule["pending"]
                                   for a in s):
        return []
    runtime = ScheduleRuntime(ScheduleConfig(steps=schedule["pending"]), world["agents"])
    runtime.revision = schedule["revision"]
    halted = {"value": world["halted"]}
    handles = SimpleNamespace(schedule=runtime, halt=lambda reason: halted.update(value=True))

    def turns(agent):
        return [(i, j) for i, step in enumerate(runtime.pending_steps)
                for j, a in enumerate(step.agents) if a == agent]

    names = sorted({*world["agents"], *py.agents()})
    before = {a: (turns(a), halted["value"]) for a in names}
    try:
        py.apply(handles, actor="test")
        error = None
    except (OpError, ValueError, RuntimeError) as exc:
        error = str(exc)
    reply = lean.ask({"cmd": "apply", "world": world, "op": op, "ok": True,
                      "ctx": {"boundary": "between_turns", "revision": 5, "agent": None,
                              "tool_call_id": None}})
    event(f"turn {op['op']}: {'error' if error else 'applied'}")
    if (reply["error"] is None) != (error is None):
        return [f"apply: lean {reply['error']}, python {error}"]
    if error is not None:
        return []
    after = {a: (turns(a), halted["value"]) for a in names}
    changed = {a for a in names if before[a] != after[a]}
    lean_changed = set(reply["changed"]) & set(names)
    return [] if changed == lean_changed else [
        f"changed: lean {sorted(lean_changed)}, python {sorted(changed)}"]


def message_ops(world):
    return ops(world, only=MESSAGE_OPS).filter(lambda o: python_op(o) is not None)


# ------------------------------------------------------------- saved examples
def _world(**overrides) -> dict[str, Any]:
    base = {"agents": ["alice", "bob", "carol"],
            "channels": [{"id": "town", "readers": ["alice", "bob", "carol"],
                          "writers": ["alice", "bob", "carol"]}],
            "routes": None,
            "messages": [{"id": "m0", "channel": "town", "sender": "alice",
                          "recipients": ["bob", "carol"], "content": "hi"}],
            "overlay": {"rewrites": [], "drops": [], "injections": [], "delivered": []},
            "schedule": {"revision": 0, "pending": [["alice"], ["bob"]]}, "halted": False}
    return {**base, **overrides}


SEAT = {"ops": None, "agents": ["alice"]}
EXAMPLES = [  # (world, scope, op): representation and gap cases kept in every run
    (_world(), SEAT, {"op": "amend_pending", "steps": [["alice"]], "schedule_revision": 0}),
    (_world(), SEAT, {"op": "replace_generation", "agent": "", "content": "x", "tool_calls": []}),
    (_world(), {"ops": [], "agents": None}, {"op": "halt"}),
    (_world(), {"ops": None, "agents": []}, {"op": "halt"}),
    (_world(), SEAT, {"op": "drop_message", "message_id": "m0", "recipients": None}),
    (_world(), SEAT, {"op": "drop_message", "message_id": "m0", "recipients": []}),
    (_world(), SEAT, {"op": "rewrite_message", "message_id": "m0", "recipients": [],
                      "content": "x"}),
    (_world(), SEAT, {"op": "rewrite_message", "message_id": "missing", "recipients": None,
                      "content": "x"}),
    (_world(), {"ops": ["rollback"], "agents": None},
     {"op": "rollback", "to_revision": 1, "interventions": [{"op": "halt"}]}),
    (_world(), SEAT, {"op": "rollback", "to_revision": 2, "interventions": [
        {"op": "drop_message", "message_id": "m0", "recipients": None}]}),
]


AT_BOB_CALL = {"boundary": "tool_call", "revision": 5, "agent": "bob", "tool_call_id": "call-1"}
CHECK_EXAMPLES = [  # (scope, ctx, op): controller checks the generators rarely reach
    (None, AT_BOB_CALL, {"op": "block_tool_call", "agent": "bob", "tool_call_id": "call-2",
                         "message": "no"}),
    (None, AT_BOB_CALL, {"op": "block_tool_call", "agent": "", "tool_call_id": None,
                         "message": "no"}),
    (SEAT, AT_BOB_CALL, {"op": "simulate_tool_result", "agent": "", "tool_call_id": None,
                         "result": "ok"}),
    (None, {"boundary": "between_turns", "revision": 5, "agent": None, "tool_call_id": None},
     {"op": "amend_pending", "steps": [["alice"], ["zed"]], "schedule_revision": 0}),
]


REPLACE = {"op": "replace_generation", "agent": "bob", "content": "x", "tool_calls": []}
DECIDE_EXAMPLES = [  # (scope, boundaries, agent, ops): batch and rollback rules
    (None, ["generation", "generation"], "bob",
     [{"op": "rollback", "to_revision": 1, "interventions": [REPLACE]}]),
    (None, ["generation"], "bob", [REPLACE, REPLACE]),
    (None, ["generation"], "bob",
     [{"op": "drop_message", "message_id": "m0", "recipients": None}, REPLACE]),
    (None, ["generation"], "bob",
     [{"op": "rollback", "to_revision": 1, "interventions": []}, REPLACE]),
    (None, ["generation"], "bob", [{"op": "rollback", "to_revision": 2, "interventions": []}]),
    (None, ["between_turns", "generation"], "bob",
     [{"op": "rollback", "to_revision": 1, "interventions": [REPLACE]}]),
    (SEAT, ["generation", "generation"], "bob",
     [{"op": "rollback", "to_revision": 1, "interventions": [REPLACE]}]),
    (SEAT, ["generation"], "alice",
     [{"op": "drop_message", "message_id": "m0", "recipients": None}]),
]


# ----------------------------------------------------------------------- tests
@pytest.mark.parametrize("scope,boundaries,agent,ops", DECIDE_EXAMPLES)
def test_saved_decide_examples(lean, scope, boundaries, agent, ops):
    assert compare_decide(lean, _world(), scope, boundaries, agent, None, ops) == []


@pytest.mark.parametrize("world,scope,op", EXAMPLES)
def test_saved_examples(lean, world, scope, op):
    assert compare_problem(lean, world, scope, op) == []


@pytest.mark.parametrize("scope,ctx,op", CHECK_EXAMPLES)
def test_saved_check_examples(lean, scope, ctx, op):
    assert compare_check(lean, _world(), scope, ctx, op) == []


@settings.get_profile(PROFILE)
@given(data=st.data())
def test_problem_and_affected(lean, data):
    world = data.draw(worlds())
    op = data.draw(ops(world))
    assume(python_op(op) is not None)
    assert compare_problem(lean, world, data.draw(scopes()), op) == []


@settings.get_profile(PROFILE)
@given(data=st.data())
def test_controller_problem(lean, data):
    world = data.draw(worlds())
    op = data.draw(ops(world))
    assume(op["op"] != "rollback" and python_op(op) is not None)
    assert compare_check(lean, world, data.draw(st.none() | scopes()),
                         data.draw(aligned_ctxs(op)), op) == []


@settings.get_profile(PROFILE)
@given(data=st.data())
def test_message_op_effects(lean, data):
    world = data.draw(worlds())
    assert compare_effects(lean, world, data.draw(message_ops(world))) == []


@settings.get_profile(PROFILE)
@given(data=st.data())
def test_frame_on_python_affected(lean, data):
    world = data.draw(worlds())
    op = data.draw(ops(world, only=tuple(n for n in OP_NAMES if n != "rollback")))
    assume(python_op(op) is not None)
    assert compare_frame(lean, world, data.draw(aligned_ctxs(op)), op,
                         data.draw(st.booleans())) == []


@settings.get_profile(PROFILE)
@given(data=st.data())
def test_turn_op_effects(lean, data):
    world = data.draw(worlds())
    op = data.draw(ops(world, only=("halt", "amend_pending")))
    assume(python_op(op) is not None)
    assert compare_turn_effects(lean, world, op) == []


@settings.get_profile(PROFILE)
@given(data=st.data())
def test_decide(lean, data):
    world = data.draw(worlds())
    boundaries = data.draw(st.lists(st.sampled_from([b.value for b in Boundary]), min_size=1,
                                    max_size=5))
    ops = data.draw(st.lists(ops_at(world, boundaries[-1]), max_size=3))
    assume(all(python_op(o) is not None for o in ops))
    agent = None if boundaries[-1] == "between_turns" else data.draw(st.sampled_from(AGENTS))
    assert compare_decide(lean, world, data.draw(st.none() | scopes()), boundaries, agent,
                          data.draw(st.none() | st.just("call-1")), ops) == []


def test_codec_keeps_null_empty_and_missing_apart(lean):
    world = _world()
    drop = {"op": "drop_message", "message_id": "m0"}
    none_ = lean.ask({"cmd": "problem", "world": world, "scope": SEAT,
                      "op": {**drop, "recipients": None}})
    empty = lean.ask({"cmd": "problem", "world": world, "scope": SEAT,
                      "op": {**drop, "recipients": []}})
    assert none_["affected"] == ["bob", "carol"] and empty["affected"] == []
    with pytest.raises(ValueError, match="missing field recipients"):
        lean.ask({"cmd": "problem", "world": world, "scope": SEAT, "op": drop})
    with pytest.raises(ValueError, match="duplicate key"):
        lean.ask_raw('{"cmd": "problem", "cmd": "check"}')


def test_codec_reads_any_unicode_name(lean):
    world = _world(agents=["😀", "😃", "é"])
    for agent in ("😃", "é", "a\\b\"c\n"):
        op = {"op": "stage_input", "agent": agent, "content": "x"}
        assert compare_problem(lean, world, {"ops": None, "agents": ["😀"]}, op) == []
        reply = lean.ask({"cmd": "problem", "world": world, "scope": SEAT, "op": op})
        assert reply["affected"] == [agent]


def test_driver_reads_every_registered_op(lean):
    world = _world(agents=[], channels=[], messages=[], schedule=None)
    examples = {
        "replace_generation": {"agent": "a", "content": "x", "tool_calls": []},
        "edit_generation": {"agent": "a", "replacements": [["x", "y"]]},
        "force_tool_call": {"agent": "a", "calls": ["f"]},
        "stage_input": {"agent": "a", "content": "x"},
        "block_tool_call": {"agent": "a", "tool_call_id": None, "message": "x"},
        "simulate_tool_result": {"agent": "a", "tool_call_id": None, "result": "x"},
        "override_tool_result": {"agent": "a", "tool_call_id": None, "result": "x"},
        "rewrite_message": {"message_id": "m", "recipients": None, "content": "x"},
        "drop_message": {"message_id": "m", "recipients": None},
        "inject_message": {"channel_id": "c", "sender": "a", "recipients": ["b"],
                           "content": "x", "override_topology": False},
        "halt": {},
        "amend_pending": {"steps": [["a"]], "schedule_revision": 0},
        "rollback": {"to_revision": 1, "interventions": []},
    }
    assert set(examples) == set(op_types()), "a registered op has no Lean counterpart"
    for name, fields in examples.items():
        reply = lean.ask({"cmd": "problem", "world": world, "op": {"op": name, **fields},
                          "scope": {"ops": None, "agents": None}})
        assert reply["problem"] is None
