"""Level 0: the auditor takes part in the run as one of its agents.

``SeatPolicy`` gives one seat of the roster to the auditor: every generation of
that agent is written by the auditor's model, from the seat's own delivered
input and tools, and applied as a ``ReplaceGeneration``. The seat is still an
ordinary Orbit agent, so it acts only when scheduled and only through its own
channels and tools (L0-1), and its output is attributed ``auditor-as:<seat>``.
Because it is a Level 1 operation underneath, Level 0 and Level 1 runs are
directly comparable (L0-2), and rollback replays a seat deterministically.
"""

from __future__ import annotations

from collections.abc import Sequence

from inspect_ai.log import transcript
from inspect_ai.model import ChatMessageSystem, GenerateConfig, Model, get_model
from inspect_ai.util import span

from orbit_audit.auditor.prompts import SEAT_SYSTEM
from orbit_audit.boundaries import Boundary
from orbit_audit.ops import ControlOp, ReplaceGeneration, ToolCallSpec
from orbit_audit.policy import AuditPolicy, BoundaryView


class SeatPolicy(AuditPolicy):
    """The auditor plays agent ``seat`` as a peer."""

    name = "seat"

    def __init__(self, *, seat: str, model: str | Model, instructions: str,
                 config: GenerateConfig | None = None) -> None:
        self.seat = seat
        self._model = model
        self.instructions = instructions
        self.config = config or GenerateConfig()
        self.tokens = 0
        self.turns: list[dict] = []

    @property
    def model(self) -> Model:
        if not isinstance(self._model, Model):
            self._model = get_model(self._model)
        return self._model

    async def decide(self, view: BoundaryView) -> Sequence[ControlOp]:
        if (view.boundary, view.agent) != (Boundary.GENERATION, self.seat):
            return []
        prompt = [ChatMessageSystem(content=SEAT_SYSTEM.format(instructions=self.instructions)),
                  *view.input]
        async with span("auditor-seat", type="orbit_audit.auditor"):
            output = await self.generate(
                self.model, prompt, tools=list(view.tool_specs), tool_choice="auto",
                config=self.config)
        if output.usage is not None:
            self.tokens += output.usage.total_tokens
        message = output.message
        allowed = {spec.name for spec in view.tool_specs}
        calls = tuple(ToolCallSpec(function=call.function, arguments=call.arguments)
                      for call in message.tool_calls or [] if call.function in allowed)
        content = message.text or ("" if calls else "(no response)")
        turn = {"branch": view.branch, "revision": view.revision, "seat": self.seat,
                "content": content,
                "tool_calls": [c.model_dump() for c in calls],
                "dropped_calls": [c.function for c in message.tool_calls or []
                                  if c.function not in allowed]}
        self.turns.append(turn)
        transcript().info({"event": "orbit_audit_seat_turn", **turn}, source="orbit_audit")
        return [ReplaceGeneration(agent=self.seat, content=content, tool_calls=calls,
                                  reason="level-0 seat")]
