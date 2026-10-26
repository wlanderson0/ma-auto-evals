"""Build a variant: a base scenario's setup inputs, edited, then rebuilt (spec §3.1–§3.3).

``apply`` never patches built config: it edits the base's ``TeamSetup`` with each
mutation in order and calls ``build_team`` again, so every prompt is generated
from the variant's own channels. Ops are registered by ``axis`` and ``op``;
an unknown one raises ``UnknownOp``, and a mutation that cannot apply (a channel
that does not exist, a reader who is already there) raises ``ApplyError``, which
the validity checks turn into a rejection (spec §3.4).

Mid-run changes: only ``schedule/activate_agent`` may have ``at="after_round:<n>"``.
It does not touch the setup; it becomes an ``Activation`` on the built scenario,
with ``after_turn = n x turns_per_round`` from the variant's own schedule, which
the scripted helper turns into an ``AmendPending`` during the run (spec §3.3).
"""

from __future__ import annotations

import copy
import dataclasses
import re
from collections.abc import Callable
from typing import Any

from orbit import ChannelConfig

from experiments.l2.model import START, Mutation, Variant
from experiments.scenarios import DOSES, Activation, Scenario, TeamSetup, build_team
from orbit_audit.scope import AuditScope

AFTER_ROUND = re.compile(r"after_round:([0-9]+)")
POSITION = re.compile(r"(after|before):([A-Za-z0-9_-]+)")
FAILURES = (ValueError, TypeError, AttributeError, KeyError)
"""What a malformed mutation can raise from Orbit's validators or the builder
(pydantic's ``ValidationError`` is a ``ValueError``); re-raised as ``ApplyError``."""


class ApplyError(ValueError):
    """A mutation cannot be applied to the scenario as it stands."""


class UnknownOp(ApplyError):
    """No op is registered for this ``axis`` and ``op``."""


def seat_scope(seat: str) -> AuditScope:
    """The scripted helper's scope without a mid-run change: the seat's opening only."""
    return AuditScope(ops={"replace_generation"}, agents={seat})


@dataclasses.dataclass
class Draft:
    """The state ``apply`` threads through the mutations."""

    team: TeamSetup
    seat: str
    intended_dose: str | None = None


Op = Callable[[Draft, dict[str, Any]], None]
OPS: dict[str, dict[str, Op]] = {}


def register(axis: str, op: str) -> Callable[[Op], Op]:
    def add(function: Op) -> Op:
        OPS.setdefault(axis, {})[op] = function
        return function

    return add


def apply(scenario: Scenario, variant: Variant) -> Scenario:
    """``scenario`` with ``variant``'s mutations applied; ``scenario`` is unchanged."""
    if scenario.team is None:
        raise ApplyError(f"{scenario.base} is not parameterised yet: it has no setup "
                         "inputs to edit")
    if variant.base != scenario.base:
        raise ApplyError(f"variant {variant.id} is defined on {variant.base}, not "
                         f"{scenario.base}")
    draft = Draft(team=scenario.team, seat=scenario.seat)
    mid_run: list[Mutation] = []
    for mutation in variant.mutations:
        function = OPS.get(mutation.axis, {}).get(mutation.op)
        if function is None:
            raise UnknownOp(f"unknown op {mutation.axis}/{mutation.op} "
                            f"(known: {sorted(f'{a}/{o}' for a in OPS for o in OPS[a])})")
        if mutation.op == "activate_agent":
            mid_run.append(mutation)
            continue
        if mutation.at != START:
            raise ApplyError(f"{mutation.axis}/{mutation.op} can only apply at the start "
                             f"(got at={mutation.at!r}); only activate_agent runs mid-run")
        try:
            function(draft, copy.deepcopy(dict(mutation.args)))
        except ApplyError:
            raise
        except FAILURES as error:
            raise ApplyError(f"{mutation.axis}/{mutation.op}: {type(error).__name__}: "
                             f"{error}") from error
    activations = tuple(activation(m, draft.team) for m in mid_run)
    name = f"{scenario.base}--{variant.id}"
    try:
        task = build_team(name, draft.team)
    except FAILURES as error:
        raise ApplyError(f"the edited setup does not build: {type(error).__name__}: "
                         f"{error}") from error
    return dataclasses.replace(
        scenario, name=name, task=task, team=draft.team,
        opening=variant.inner.opening, activations=activations,
        intended_dose=draft.intended_dose, scope=inner_scope(scenario, variant),
        # Plan checks judge an L1 auditor's branches against the base's brief (e.g. the
        # Kestrel doses in rollback order); they do not fit a variant (spec §3.1), whose
        # seat dose is measured from its own posts instead.
        plan_check=None,
        variant=recorded(variant))


def activation(mutation: Mutation, team: TeamSetup) -> Activation:
    """``after_round:<n>`` as the turn count at that pause, from the variant's schedule."""
    match = AFTER_ROUND.fullmatch(mutation.at)
    if match is None:
        raise ApplyError(f"activate_agent needs at='after_round:<n>', got {mutation.at!r}")
    args = _args(dict(mutation.args), {"agent", "position"}, "activate_agent")
    if not POSITION.fullmatch(str(args["position"])):
        raise ApplyError(f"activate_agent position must be 'after:<agent>' or "
                         f"'before:<agent>', got {args['position']!r}")
    try:
        turns = team.turns_per_round()
    except ValueError as error:
        raise ApplyError(f"activate_agent needs rounds: {error}") from error
    return Activation(agent=args["agent"], after_turn=int(match[1]) * turns,
                      position=args["position"])


def inner_scope(scenario: Scenario, variant: Variant) -> AuditScope | None:
    """The inner policy's scope: the variant's own, else the condition's default."""
    if variant.inner.scope is not None:
        return variant.inner.scope
    if variant.inner.condition == "scripted":
        return seat_scope(scenario.seat)
    return scenario.scope


def recorded(variant: Variant) -> dict[str, Any]:
    """The variant as plain JSON-able data, for logs and metadata."""
    inner = variant.inner
    return {
        "id": variant.id, "base": variant.base, "rationale": variant.rationale,
        "mutations": [dataclasses.asdict(m) for m in variant.mutations],
        "inner": {
            "condition": inner.condition, "brief": inner.brief,
            "opening": [list(message) for message in inner.opening],
            "scope": None if inner.scope is None else {  # sorted: frozensets dump unordered
                key: None if value is None else sorted(value)
                for key, value in (("ops", inner.scope.ops), ("agents", inner.scope.agents))},
            "limits": None if inner.limits is None else inner.limits.model_dump(mode="json"),
        },
    }


# ------------------------------------------------------------------- helpers
def _args(args: dict[str, Any], required: set[str], op: str,
          optional: set[str] = frozenset()) -> dict[str, Any]:
    if missing := sorted(required - set(args)):
        raise ApplyError(f"{op} needs {missing}")
    if unknown := sorted(set(args) - required - set(optional)):
        raise ApplyError(f"{op} does not take {unknown}")
    return args


def _text(args: dict[str, Any], key: str, op: str) -> str:
    value = args[key]
    if not isinstance(value, str) or not value.strip():
        raise ApplyError(f"{op}: {key} must be non-empty text, got {value!r}")
    return value.strip()


def _channel(team: TeamSetup, channel: str) -> ChannelConfig:
    for config in team.channels:
        if config.id == channel:
            return config
    raise ApplyError(f"no channel {channel!r} (channels: {[c.id for c in team.channels]})")


def _replace_channel(team: TeamSetup, channel: str, **lists: tuple[str, ...]) -> TeamSetup:
    channels = tuple(
        ChannelConfig.model_validate({**c.model_dump(), **{k: list(v) for k, v in lists.items()}})
        if c.id == channel else c for c in team.channels)
    return dataclasses.replace(team, channels=channels)


def _membership(field: str, add: bool) -> Op:
    word = field[:-1]  # "reader" / "writer"

    def change(draft: Draft, args: dict[str, Any]) -> None:
        op = f"{'add' if add else 'remove'}_{word}"
        _args(args, {"agent", "channel"}, op)
        agent, channel = _text(args, "agent", op), _text(args, "channel", op)
        members = tuple(getattr(_channel(draft.team, channel), field))
        if add and agent in members:
            raise ApplyError(f"{agent} is already a {word} of {channel!r}")
        if not add and agent not in members:
            raise ApplyError(f"{agent} is not a {word} of {channel!r}")
        updated = (*members, agent) if add else tuple(a for a in members if a != agent)
        draft.team = _replace_channel(draft.team, channel, **{field: updated})

    return change


for _field in ("readers", "writers"):
    for _add in (True, False):
        register("topology", f"{'add' if _add else 'remove'}_{_field[:-1]}")(
            _membership(_field, _add))


@register("schedule", "set_rounds")
def set_rounds(draft: Draft, args: dict[str, Any]) -> None:
    _args(args, {"rounds"}, "set_rounds")
    rounds = args["rounds"]
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
        raise ApplyError(f"set_rounds needs a positive whole number, got {rounds!r}")
    if draft.team.schedule is not None:
        raise ApplyError("set_rounds cannot change an explicit schedule")
    draft.team = dataclasses.replace(draft.team, rounds=rounds)


@register("schedule", "activate_agent")
def activate_agent(draft: Draft, args: dict[str, Any]) -> None:
    """Never called on the setup: ``apply`` turns the mutation into an ``Activation``."""
    raise AssertionError("activate_agent is applied mid-run")


@register("roster", "add_agent")
def add_agent(draft: Draft, args: dict[str, Any]) -> None:
    """Add an agent to the setup and its channels, with no turns (spec §3.3).

    ``model: null`` leaves it on the task's default model (as offline tests do)."""
    _args(args, {"name", "persona", "model", "channels"}, "add_agent",
          optional={"extra", "turns"})
    name, persona = _text(args, "name", "add_agent"), _text(args, "persona", "add_agent")
    if args["model"] is not None:
        _text(args, "model", "add_agent")
    if args.get("extra") is not None:
        _text(args, "extra", "add_agent")
    team = draft.team
    if name in team.roster:
        raise ApplyError(f"{name} is already in the setup")
    if args.get("turns", False):
        raise ApplyError("add_agent with turns is not supported: add it with turns: false "
                         "and schedule it with activate_agent")
    channels = args["channels"]
    if not isinstance(channels, dict) or set(channels) - {"read", "write"}:
        raise ApplyError(f"add_agent channels must be {{read: [...], write: [...]}}, "
                         f"got {channels!r}")
    team = dataclasses.replace(
        team, roster=(*team.roster, name), personas={**team.personas, name: persona},
        models={**team.models, name: args["model"]},
        extra={**team.extra, name: args["extra"]} if args.get("extra") else team.extra)
    for key, field in (("read", "readers"), ("write", "writers")):
        listed = channels.get(key) or []
        if not isinstance(listed, list) or not all(isinstance(c, str) for c in listed):
            raise ApplyError(f"add_agent channels.{key} must be a list of channel ids")
        if len(set(listed)) != len(listed):
            raise ApplyError(f"add_agent channels.{key} lists a channel twice: {listed}")
        for channel in listed:
            members = tuple(getattr(_channel(team, channel), field))
            if name in members:
                raise ApplyError(f"{name} is already a {field[:-1]} of {channel!r}")
            team = _replace_channel(team, channel, **{field: (*members, name)})
    draft.team = team


@register("persona", "append_line")
def append_line(draft: Draft, args: dict[str, Any]) -> None:
    """Add a sentence to the agent's extra prompt text (never to its persona phrase)."""
    _args(args, {"agent", "line"}, "append_line", optional={"dose"})
    agent, line = _text(args, "agent", "append_line"), _text(args, "line", "append_line")
    if agent not in draft.team.roster:
        raise ApplyError(f"{agent} is not in the setup")
    if "dose" in args:
        dose = args["dose"]
        if dose not in DOSES:
            raise ApplyError(f"append_line dose must be one of {list(DOSES)}, got {dose!r}")
        if agent != draft.seat:
            raise ApplyError(f"a dose is the seat's ({draft.seat}), not {agent}'s")
        if draft.intended_dose is not None:
            raise ApplyError(f"two doses in one variant ({draft.intended_dose}, {dose})")
        draft.intended_dose = dose
    current = draft.team.extra.get(agent, "")
    draft.team = dataclasses.replace(
        draft.team, extra={**draft.team.extra, agent: f"{current} {line}" if current else line})
