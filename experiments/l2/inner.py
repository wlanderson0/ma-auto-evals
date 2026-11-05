"""The ``scripted`` inner condition: the seat's fixed opening, then the seat itself
(spec ``docs/level2-interface.md`` §4.1), plus the mid-run helper (§3.3).

``scripted_policy`` reads only a built scenario's typed fields (``seat``,
``opening``, ``activations``), never its recorded ``variant`` diff:

- each opening message becomes the seat's ``i``-th generation, written as one
  ``channel_post`` call on its channel (agents only talk through that tool);
  every later generation is the seat's own model;
- each ``Activation`` becomes a rule that, at the between-turns pause after
  ``after_turn`` turns (``view.turn == after_turn - 1``: the controller numbers
  turns from 0), amends the pending schedule: the agent is inserted at its
  position in every remaining round. The ``AmendPending`` is built from what the
  helper sees then (the pending steps and their schedule revision), since the
  revision only exists during the run.

The helper is a fixed piece of code, not a model, and makes no model calls. If the
controller refuses one of its operations (e.g. a scope without ``amend_pending``),
it raises ``HelperRefused``, which errors the sample: a variant whose opening or
activation did not happen must not be read as a run of that variant.
"""

from __future__ import annotations

from experiments.scenarios import Activation, Scenario
from orbit_audit.boundaries import Boundary
from orbit_audit.evidence import InterventionRecord
from orbit_audit.ops import AmendPending, ReplaceGeneration, ToolCallSpec
from orbit_audit.policy import BoundaryView, Rule, ScriptedPolicy, at_generation

OPENING_REASON = "scripted opening"
ACTIVATION_REASON = "scripted activation"


def opening_op(seat: str, channel: str, text: str) -> ReplaceGeneration:
    return ReplaceGeneration(
        agent=seat, reason=OPENING_REASON,
        tool_calls=(ToolCallSpec(function="channel_post",
                                 arguments={"channel_id": channel, "message": text}),))


def with_agent(steps: tuple[tuple[str, ...], ...], activation: Activation
               ) -> tuple[tuple[str, ...], ...]:
    """``steps`` with ``(agent,)`` inserted after (or before) every step that is
    exactly ``(anchor,)``. Raises if the anchor never appears: the activation
    would silently do nothing."""
    where, anchor = activation.position.split(":", 1)
    result: list[tuple[str, ...]] = []
    for step in steps:
        if step == (anchor,) and where == "before":
            result.append((activation.agent,))
        result.append(step)
        if step == (anchor,) and where == "after":
            result.append((activation.agent,))
    if len(result) == len(steps):
        raise ValueError(f"cannot activate {activation.agent}: {anchor!r} is not in the "
                         f"pending schedule {steps}")
    return tuple(result)


def activation_rule(activation: Activation) -> Rule:
    # ``view.turn`` is the 0-based index of the turn just finished, so the pause after
    # ``after_turn`` turns has ``view.turn == after_turn - 1`` (pinned by a test).
    pause = activation.after_turn - 1

    def rule(view: BoundaryView) -> AmendPending | None:
        if (view.boundary, view.turn) != (Boundary.BETWEEN_TURNS, pause):
            return None
        return AmendPending(steps=with_agent(view.pending_steps, activation),
                            schedule_revision=view.schedule_revision,
                            reason=f"{ACTIVATION_REASON}: {activation.agent} "
                                   f"{activation.position}")

    return rule


class HelperRefused(RuntimeError):
    """The controller refused one of the scripted helper's operations."""


class ScriptedHelper(ScriptedPolicy):
    """A ``ScriptedPolicy`` that fails loudly when one of its operations is refused."""

    def notice(self, record: InterventionRecord) -> None:
        if record.outcome == "rejected" and not record.replayed:
            raise HelperRefused(f"the scripted helper's {record.op} at turn {record.turn} was "
                                f"refused: {record.reason}")


def scripted_policy(built: Scenario) -> ScriptedHelper:
    """The scripted helper for ``built``: its opening, then its activations."""
    rules: list[Rule] = [at_generation(built.seat, i, opening_op(built.seat, channel, text))
                         for i, (channel, text) in enumerate(built.opening)]
    rules.extend(activation_rule(activation) for activation in built.activations)
    return ScriptedHelper(*rules)
