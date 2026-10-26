"""Validity checks for a built variant (spec ``docs/level2-interface.md`` §3.4).

A rejection means the variant is never run (it is still recorded, with its
reasons); rejections are kept for what would crash, cannot mean anything, or the
code cannot do yet. A warning means it runs, with the warning attached to its
results. The checks read the built scenario (its config, ``opening``,
``activations`` and ``scope``), and the variant only for its inner condition and
which axes it changes.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from experiments.l2.model import START, Problems, Variant
from experiments.scenarios import Scenario

MID_RUN_CONDITIONS = ("scripted",)
"""Conditions whose policy can apply a mid-run change: only the scripted helper does
in milestone 1 (an L0/L1 auditor needs the combined policy, spec §3.3; a baseline
helper is not built yet)."""
SETUP_AXES = ("topology", "boards", "schedule", "roster", "task")
"""Changes that make the base's auditor goal text untrue (it states the channels,
rounds and who can reach whom); persona and model changes do not."""


def check(base: Scenario, built: Scenario | None, variant: Variant,
          build_error: str | None = None) -> Problems:
    """What stops ``variant`` (built from ``base``) from running, and what looks off."""
    if build_error is not None:
        return Problems(rejections=(f"the variant does not build: {build_error}",))
    if built is None:
        return Problems(rejections=("the variant was not built",))
    rejections: list[str] = []
    warnings: list[str] = []
    task = built.task
    agents = {spec.name for spec in task["setup"].agents}
    channels = list(task["communication"].channels)
    scheduled = {agent for step in task["execution"].plan.steps for agent in step.agents}
    activated = [activation.agent for activation in built.activations]
    condition = variant.inner.condition
    scope = built.scope

    # ------------------------------------------------------------ rejections
    for config in channels:
        for role in ("readers", "writers"):
            if unknown := sorted(set(getattr(config, role)) - agents):
                rejections.append(f"channel {config.id!r} names {role[:-1]}(s) {unknown} "
                                  "who are not in the setup")
    output = task["execution"].output_agent
    if output is not None and output not in scheduled:
        rejections.append(f"the output agent {output!r} is not in the schedule")

    if built.opening and condition != "scripted":
        rejections.append(f"an opening is only played by the scripted condition, not "
                          f"{condition!r}")
    writable = {c.id for c in channels if built.seat in c.writers}
    for channel, _ in built.opening:
        if channel not in writable:
            rejections.append(f"the opening posts on {channel!r}, which the seat "
                              f"{built.seat!r} cannot write to in this variant")
    if built.opening and scope is not None and not (
            scope.allows_op("replace_generation")
            and (scope.agents is None or built.seat in scope.agents)):
        rejections.append(f"the inner scope ({scope.describe()}) would refuse the opening, "
                          f"a replace_generation on the seat {built.seat!r}")

    if condition in ("l0", "l1") and (
            changed := sorted({m.axis for m in variant.mutations} & set(SETUP_AXES))):
        rejections.append(f"an inner {condition} auditor would get the base's goal, which "
                          f"describes the base's setup, not this variant's ({changed} "
                          "changed); goals generated from the variant's setup are not built "
                          "yet (spec §3.1)")
    mid_run = bool(built.activations) or any(m.at != START for m in variant.mutations)
    if mid_run and condition in ("l0", "l1"):
        rejections.append(f"mid-run changes cannot run with an inner {condition} auditor "
                          "until the combined policy exists (spec §3.3)")
    elif mid_run and condition not in MID_RUN_CONDITIONS:
        rejections.append(f"mid-run changes need the scripted helper; the {condition} "
                          "condition has none yet")
    if built.activations and condition in MID_RUN_CONDITIONS and scope is not None and not (
            scope.allows_op("amend_pending") and scope.agents is None):
        rejections.append("the inner scope must allow amend_pending with agents: null (an "
                          "amendment affects every agent in the pending schedule), e.g. "
                          "{ops: [replace_generation, amend_pending], agents: null}, as its "
                          "control should too")
    if len(activated) > 1:
        # Two at one pause would share a schedule revision (Orbit refuses the second);
        # at different rounds, the first makes later rounds longer, so the second's
        # turn count no longer lands on a round boundary.
        rejections.append(f"milestone 1 supports one activation per variant, not "
                          f"{len(activated)} ({activated})")
    rounds = built.team.rounds if built.team is not None else None
    for activation in built.activations:
        agent = activation.agent
        if agent not in agents:
            rejections.append(f"activate_agent: {agent!r} is not in the setup")
        elif agent in scheduled:
            rejections.append(f"activate_agent: {agent!r} is already in the schedule")
        anchor = activation.position.partition(":")[2]
        if anchor not in scheduled:
            rejections.append(f"activate_agent: its position names {anchor!r}, who is not "
                              "in the schedule, so the agent would never be placed")
        if rounds is None:
            rejections.append("activate_agent needs a schedule in rounds")
            continue
        after_round = activation.after_turn // built.team.turns_per_round()
        if rounds < 2:
            rejections.append(f"activate_agent cannot take effect: the variant has only "
                              f"{rounds} round")
        elif not 1 <= after_round < rounds:
            rejections.append(f"activate_agent after round {after_round} never takes effect: "
                              f"it must come after round 1 to {rounds - 1} of {rounds}")

    # -------------------------------------------------------------- warnings
    for agent in sorted(scheduled | set(activated)):
        if not any(agent in c.readers or agent in c.writers for c in channels):
            warnings.append(f"{agent!r} is scheduled but can read and write nothing")
    for agent in (built.seat, *built.targets):
        if agent not in scheduled:
            kind = "seat" if agent == built.seat else "target"
            warnings.append(f"the {kind} {agent!r} is never scheduled")
    reach = reachable(built.seat, channels, relays=scheduled | set(activated))
    for target in built.targets:
        if target not in reach:
            warnings.append(f"the target {target!r} cannot be reached from the seat "
                            f"{built.seat!r}")
    return Problems(rejections=_unique(rejections), warnings=_unique(warnings))


def reachable(seat: str, channels: list, relays: Iterable[str] | None = None) -> set[str]:
    """Agents a message from ``seat`` can reach (BFS: the seat writes c, readers of c
    write c', readers of c' ...), relayed only by agents in ``relays`` (agents that
    ever act; ``None``: anyone). The seat itself is excluded."""
    relaying = None if relays is None else set(relays)
    seen, queue = {seat}, deque([seat])
    while queue:
        agent = queue.popleft()
        if agent != seat and relaying is not None and agent not in relaying:
            continue  # reached, but never acts, so passes nothing on
        for config in channels:
            if agent in config.writers:
                for reader in config.readers:
                    if reader not in seen:
                        seen.add(reader)
                        queue.append(reader)
    return seen - {seat}


def _unique(messages: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(messages))
