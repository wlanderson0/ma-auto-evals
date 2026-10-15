"""Typed control operations an auditor can apply at lifecycle boundaries.

Each operation is a frozen, versioned pydantic model that declares the
boundaries where it is legal (§5.5). The controller validates an operation
fully before any effect and rejects it, with a recorded reason, if it targets
the wrong agent or boundary, carries a stale ``expected_revision`` or has an
invalid payload. Researchers can define new operations by subclassing one of
the boundary families below and registering them with ``register_op``.

Families:

- ``GenerationOp`` (``Boundary.GENERATION``): produce an agent's generation,
  with or without calling its model.
- ``ActivationOp`` (``Boundary.BEFORE_ACTIVATION``): stage durable input into
  an agent's conversation before it runs.
- ``ToolOp`` (``Boundary.TOOL_CALL`` / ``Boundary.TOOL_RESULT``): block or
  simulate a proposed tool call, or override an executed call's result.
- ``MessageOp`` (any boundary): rewrite, drop or inject agent-to-agent channel
  messages for chosen recipients, through a delivery overlay.
- ``TurnOp`` (``Boundary.BETWEEN_TURNS``): change pending activations or halt.
- ``Rollback`` (any boundary): fork a new branch from an earlier revision with
  interventions, ending the current branch (``orbit_audit.branches``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    ContentText,
    ModelOutput,
)
from inspect_ai.tool import ToolCall, ToolInfo
from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator

from orbit_audit.boundaries import Boundary
from orbit_audit.evidence import AUDITOR, auditor_as, tag

if TYPE_CHECKING:
    from orbit.execution.control import ActivationContext, GenerationRequest, RuntimeHandles

    from orbit_audit.delivery import CommunicationWorld, DeliveryOverlay


class OpError(ValueError):
    """An operation's payload is invalid for the state it was applied to.

    ``provider_output`` is set when the agent's model already ran before the
    problem was found (for example an edit whose text is absent); the agent then
    receives that output unmodified rather than a second generation.
    """

    def __init__(self, message: str, *, provider_output: ModelOutput | None = None) -> None:
        super().__init__(message)
        self.provider_output = provider_output


class ControlOp(BaseModel):
    """Base class: a typed, versioned request to change the run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ClassVar[str]
    version: ClassVar[int] = 1
    boundaries: ClassVar[frozenset[Boundary]]

    expected_revision: int | None = None
    """Audit revision the operation was prepared against; ``None`` skips the check."""
    reason: str = ""

    def agents(self) -> tuple[str, ...]:
        """Agents whose state or output this operation affects."""
        agent = getattr(self, "agent", None)
        return (agent,) if agent else ()

    def affected(self, world: CommunicationWorld | None) -> tuple[str, ...]:
        """``agents()``, resolved against ``world`` where the op leaves recipients implicit."""
        return self.agents()

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"expected_revision", "reason"})


# ----------------------------------------------------------------- generation
Proceed = Callable[..., Awaitable[ModelOutput]]


class GenerationOp(ControlOp):
    boundaries: ClassVar[frozenset[Boundary]] = frozenset({Boundary.GENERATION})

    agent: str

    async def produce(
        self, request: GenerationRequest, proceed: Proceed
    ) -> tuple[ModelOutput, ModelOutput | None]:
        """Return ``(output for the agent, provider output if the model ran)``."""
        raise NotImplementedError


class ToolCallSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    function: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ReplaceGeneration(GenerationOp):
    """Write the agent's whole generation; its model is not called."""

    name: ClassVar[str] = "replace_generation"

    content: str = ""
    tool_calls: tuple[ToolCallSpec, ...] = ()

    async def produce(self, request, proceed):
        if not self.content and not self.tool_calls:
            raise OpError("replace_generation needs content or tool_calls")
        calls = [_validated_call(spec, request.tools) for spec in self.tool_calls]
        message = ChatMessageAssistant(
            id=_message_id(), content=self.content, tool_calls=calls or None, source="generate",
        )
        output = ModelOutput.from_message(message, stop_reason="tool_calls" if calls else "stop")
        output.choices[0].message = tag(output.message, auditor_as(self.agent))
        return output, None


class TextReplacement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    old: str = Field(min_length=1)
    new: str


class EditGeneration(GenerationOp):
    """Let the model generate, then replace exact substrings in its output.

    Applies to message text and to string-valued tool-call arguments. Every
    ``old`` must occur in the generation, otherwise the edit is rejected and the
    agent receives the unmodified model output.
    """

    name: ClassVar[str] = "edit_generation"

    replacements: tuple[TextReplacement, ...] = Field(min_length=1)

    async def produce(self, request, proceed):
        original = await proceed()
        edited = original.model_copy(deep=True)
        message = edited.message
        try:
            for replacement in self.replacements:
                if not _replace_in_message(message, replacement):
                    raise OpError(f"edit_generation: {replacement.old!r} not found in generation")
            for call in message.tool_calls or []:
                _validated_call(ToolCallSpec(function=call.function, arguments=call.arguments),
                                request.tools)
        except OpError as error:
            raise OpError(str(error), provider_output=original) from None
        edited.choices[0].message = tag(message, auditor_as(self.agent))
        return edited, original


class ForceToolCall(GenerationOp):
    """Let the model generate, keep its text, and replace its tool calls."""

    name: ClassVar[str] = "force_tool_call"

    calls: tuple[ToolCallSpec, ...] = Field(min_length=1)

    async def produce(self, request, proceed):
        forced = [_validated_call(spec, request.tools) for spec in self.calls]
        original = await proceed()
        edited = original.model_copy(deep=True)
        edited.message.tool_calls = forced
        edited.choices[0].stop_reason = "tool_calls"
        edited.choices[0].message = tag(edited.message, auditor_as(self.agent))
        return edited, original


# ----------------------------------------------------------------- activation
class ActivationOp(ControlOp):
    boundaries: ClassVar[frozenset[Boundary]] = frozenset({Boundary.BEFORE_ACTIVATION})

    agent: str

    def apply(self, activation: ActivationContext) -> Any:
        """Mutate the activation's conversation; return what was added."""
        raise NotImplementedError


class StageInput(ActivationOp):
    """Append environment or user input to an agent's conversation.

    The message is durable context, authored ``auditor``. It does not claim to
    come from another agent; messages *as* an agent go through their channel.
    """

    name: ClassVar[str] = "stage_input"

    content: str = Field(min_length=1)
    role: Literal["user", "system"] = "user"
    untrusted: bool = True
    """Mark as an untrusted observation, like Orbit's own injected context."""

    def apply(self, activation):
        cls = ChatMessageUser if self.role == "user" else ChatMessageSystem
        metadata = {"untrusted_observation": True} if self.untrusted else {}
        message = tag(cls(id=_message_id(), content=self.content, metadata=metadata), AUDITOR)
        activation.state.messages.append(message)
        return message.model_dump(mode="json")


# ---------------------------------------------------------------------- tools
class ToolOp(ControlOp):
    agent: str
    tool_call_id: str | None = None
    """If set, the operation applies only to this exact tool call."""


class BlockToolCall(ToolOp):
    """Refuse a proposed tool call; the agent sees an ordinary tool error."""

    name: ClassVar[str] = "block_tool_call"
    boundaries: ClassVar[frozenset[Boundary]] = frozenset({Boundary.TOOL_CALL})

    message: str = Field(default="The tool call was not permitted.", min_length=1)


class SimulateToolResult(ToolOp):
    """Answer a proposed tool call without executing it (no side effect)."""

    name: ClassVar[str] = "simulate_tool_result"
    boundaries: ClassVar[frozenset[Boundary]] = frozenset({Boundary.TOOL_CALL})

    result: str


class OverrideToolResult(ToolOp):
    """Replace an executed tool call's result before the agent sees it."""

    name: ClassVar[str] = "override_tool_result"
    boundaries: ClassVar[frozenset[Boundary]] = frozenset({Boundary.TOOL_RESULT})

    result: str


# ------------------------------------------------------------------- messages
class MessageOp(ControlOp):
    """Change what recipients receive on channels; canonical messages never change."""

    boundaries: ClassVar[frozenset[Boundary]] = frozenset(Boundary)

    def apply(self, overlay: DeliveryOverlay, world: CommunicationWorld) -> dict[str, Any]:
        """Validate against ``world``, update ``overlay``; return evidence fields."""
        raise NotImplementedError


class _ExistingMessageOp(MessageOp):
    message_id: str
    recipients: tuple[str, ...] | None = None
    """Recipients whose view changes; ``None`` means every intended recipient."""

    def agents(self) -> tuple[str, ...]:
        return tuple(sorted(self.recipients or ()))

    def affected(self, world: CommunicationWorld | None) -> tuple[str, ...]:
        """With ``recipients=None``, every intended recipient of the message.

        Unresolvable here (no world, or no such message yet) falls back to
        ``agents()``; the op is validated again where it applies.
        """
        from orbit_audit.delivery import recipients_or_all

        message = world.messages.get(self.message_id) if world else None
        if message is None:
            return self.agents()
        return tuple(sorted(recipients_or_all(message, self.recipients) - {message.sender}))

    def _target(self, overlay: DeliveryOverlay, world: CommunicationWorld):
        from orbit_audit.delivery import recipients_or_all

        message = world.messages.get(self.message_id)
        if message is None:
            raise OpError(f"no channel message {self.message_id!r}")
        targets = recipients_or_all(message, self.recipients)
        outside = targets - set(message.recipients)
        if outside:
            raise OpError(f"{sorted(outside)} are not recipients of {self.message_id!r}")
        seen = targets & overlay.delivered_to(self.message_id)
        if seen:
            raise OpError(
                f"{self.message_id!r} was already delivered to {sorted(seen)}; "
                "a delivered message cannot be changed (roll back instead)"
            )
        keys = [(self.message_id, None)] if self.recipients is None else [
            (self.message_id, recipient) for recipient in sorted(targets)]
        return message, keys


class RewriteMessage(_ExistingMessageOp):
    """Deliver different content for a message, still attributed to its sender."""

    name: ClassVar[str] = "rewrite_message"

    content: str = Field(min_length=1)

    def affected(self, world):
        """The recipients, and the sender the new content is attributed to."""
        message = world.messages.get(self.message_id) if world else None
        sender = {message.sender} if message else set()
        return tuple(sorted({*super().affected(world), *sender}))

    def apply(self, overlay, world):
        message, keys = self._target(overlay, world)
        for key in keys:
            overlay.rewrites[key] = self.content
        return {"author": auditor_as(message.sender), "original": message.content,
                "replacement": self.content}


class DropMessage(_ExistingMessageOp):
    """Withhold a message from recipients; the sender's own view is unchanged."""

    name: ClassVar[str] = "drop_message"

    def apply(self, overlay, world):
        message, keys = self._target(overlay, world)
        overlay.drops.update(keys)
        return {"original": message.content}


class InjectMessage(MessageOp):
    """Deliver a new message attributed to ``sender`` that ``sender`` never sent.

    Respects the channel's writers/readers and the setup's communication edges
    unless ``override_topology`` is set; overrides are recorded as such.
    """

    name: ClassVar[str] = "inject_message"

    channel_id: str
    sender: str
    content: str = Field(min_length=1)
    recipients: tuple[str, ...] = Field(min_length=1)
    override_topology: bool = False

    def agents(self) -> tuple[str, ...]:
        return tuple(sorted({self.sender, *self.recipients}))

    def apply(self, overlay, world):
        from orbit.communication import CommunicationMessage, allocate_message_sequence
        from orbit.ids import new_id_hex

        channel = world.channels.get(self.channel_id)
        if channel is None:
            raise OpError(f"no channel {self.channel_id!r}")
        if self.sender in self.recipients:
            raise OpError("an injected message cannot be addressed to its own sender")
        violations = []
        if self.sender not in world.writers(channel):
            violations.append(f"{self.sender} cannot write to {self.channel_id}")
        for recipient in self.recipients:
            if recipient not in world.readers(channel):
                violations.append(f"{recipient} cannot read {self.channel_id}")
            elif not world.route_allowed(self.sender, recipient):
                violations.append(f"no route {self.sender}->{recipient}")
        if violations and not self.override_topology:
            raise OpError(
                "off-topology injection needs override_topology: " + "; ".join(violations))
        message = CommunicationMessage(
            id=f"message-{new_id_hex()}", channel_id=self.channel_id, sender=self.sender,
            recipients=tuple(self.recipients), content=self.content,
            sequence=allocate_message_sequence(),
        )
        overlay.injections.append(message)
        return {"author": auditor_as(self.sender), "replacement": message.model_dump(mode="json"),
                "details": {"topology_violations": violations}}


# ---------------------------------------------------------------------- turns
class TurnOp(ControlOp):
    boundaries: ClassVar[frozenset[Boundary]] = frozenset({Boundary.BETWEEN_TURNS})

    def apply(self, runtime: RuntimeHandles, *, actor: str) -> Any:
        raise NotImplementedError


class Halt(TurnOp):
    """Stop the run before the next turn."""

    name: ClassVar[str] = "halt"

    def apply(self, runtime, *, actor):
        runtime.halt(self.reason or "audit complete")


class AmendPending(TurnOp):
    """Replace the activation plan's pending (unstarted) steps."""

    name: ClassVar[str] = "amend_pending"

    steps: tuple[tuple[str, ...], ...] = Field(min_length=1)
    schedule_revision: int
    """``ScheduleRuntime.revision`` the amendment was prepared against."""

    def agents(self) -> tuple[str, ...]:
        return tuple(sorted({agent for step in self.steps for agent in step}))

    def apply(self, runtime, *, actor):
        schedule = runtime.schedule
        if schedule is None:
            raise OpError("amend_pending requires an explicit activation plan")
        before = [step.model_dump(mode="json") for step in schedule.pending_steps]
        schedule.amend_pending(
            [list(step) for step in self.steps], actor=actor,
            reason=self.reason or "auditor amendment", expected_revision=self.schedule_revision,
        )
        return {"before": before}


# ------------------------------------------------------------------ branches
class Rollback(ControlOp):
    """Fork a new branch that replays this one up to ``to_revision``.

    At ``to_revision`` the new branch applies ``interventions`` (validated
    there like any operation), then continues live under the same policy. The
    current branch ends. It must be the only operation in its batch.
    """

    name: ClassVar[str] = "rollback"
    boundaries: ClassVar[frozenset[Boundary]] = frozenset(Boundary)

    to_revision: int = Field(ge=1)
    interventions: tuple[SerializeAsAny[ControlOp], ...] = ()

    @field_validator("interventions", mode="before")
    @classmethod
    def _parse(cls, value: Any) -> Any:
        ops = [parse_op(item) if isinstance(item, dict) else item for item in value or ()]
        if any(isinstance(op, Rollback) for op in ops):
            raise ValueError("a rollback cannot contain another rollback")
        return tuple(ops)

    def agents(self) -> tuple[str, ...]:
        return tuple(sorted({agent for op in self.interventions for agent in op.agents()}))

    def payload(self) -> dict[str, Any]:
        return {"to_revision": self.to_revision,
                "interventions": [{"op": op.name, **op.payload()} for op in self.interventions]}


# -------------------------------------------------------------------- registry
_REGISTRY: dict[str, type[ControlOp]] = {}


def register_op(cls: type[ControlOp]) -> type[ControlOp]:
    """Register an operation type by its ``name`` (usable as a decorator)."""
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        raise ValueError(f"control operation {cls.name!r} is already registered")
    _REGISTRY[cls.name] = cls
    return cls


def op_types() -> dict[str, type[ControlOp]]:
    return dict(_REGISTRY)


def parse_op(data: dict[str, Any]) -> ControlOp:
    """Build an operation from ``{"op": name, ...fields}``; unknown names fail."""
    fields = dict(data)
    name = fields.pop("op", None)
    if name not in _REGISTRY:
        raise OpError(f"unknown control operation {name!r}")
    return _REGISTRY[name].model_validate(fields)


for _cls in (ReplaceGeneration, EditGeneration, ForceToolCall, StageInput, BlockToolCall,
             SimulateToolResult, OverrideToolResult, RewriteMessage, DropMessage, InjectMessage,
             Halt, AmendPending, Rollback):
    register_op(_cls)


# -------------------------------------------------------------------- helpers
def tool_schemas(tools: Any) -> dict[str, ToolInfo]:
    """Name -> ToolInfo for the tools offered in a generation request."""
    from inspect_ai.tool import ToolDef

    schemas: dict[str, ToolInfo] = {}
    for tool in tools or []:
        if isinstance(tool, ToolInfo):
            schemas[tool.name] = tool
            continue
        definition = tool if isinstance(tool, ToolDef) else ToolDef(tool)
        schemas[definition.name] = ToolInfo(
            name=definition.name, description=definition.description,
            parameters=definition.parameters,
        )
    return schemas


def _message_id() -> str:
    """Deterministic under ``orbit.ids.id_scope``, unlike Inspect's default IDs."""
    from orbit.ids import new_id_hex

    return f"message-{new_id_hex()}"


def _validated_call(spec: ToolCallSpec, tools: Any) -> ToolCall:
    schemas = tool_schemas(tools)
    info = schemas.get(spec.function)
    if info is None:
        raise OpError(
            f"tool {spec.function!r} is not available to this agent "
            f"(available: {sorted(schemas)})"
        )
    params = info.parameters
    missing = set(params.required) - set(spec.arguments)
    unknown = set(spec.arguments) - set(params.properties)
    if missing:
        raise OpError(f"tool {spec.function!r} missing required arguments {sorted(missing)}")
    if unknown and params.additionalProperties is False:
        raise OpError(f"tool {spec.function!r} got unknown arguments {sorted(unknown)}")
    from orbit.ids import new_id_hex

    return ToolCall(id=f"call_{new_id_hex()}", function=spec.function,
                    arguments=dict(spec.arguments))


def _replace_in_message(message: ChatMessageAssistant, replacement: TextReplacement) -> bool:
    found = False
    if isinstance(message.content, str):
        if replacement.old in message.content:
            message.content = message.content.replace(replacement.old, replacement.new)
            found = True
    else:
        for part in message.content:
            if isinstance(part, ContentText) and replacement.old in part.text:
                part.text = part.text.replace(replacement.old, replacement.new)
                found = True
    for call in message.tool_calls or []:
        for key, value in call.arguments.items():
            if isinstance(value, str) and replacement.old in value:
                call.arguments[key] = value.replace(replacement.old, replacement.new)
                found = True
    return found


def summarize(output: ModelOutput | None) -> dict[str, Any] | None:
    """Compact JSON form of a generation for evidence records."""
    if output is None:
        return None
    message = output.message
    return {
        "content": message.text,
        "tool_calls": [
            {"id": c.id, "function": c.function, "arguments": c.arguments}
            for c in (message.tool_calls or [])
        ],
    }


def ops_for(boundary: Boundary, ops: Sequence[ControlOp]) -> list[ControlOp]:
    return [op for op in ops if boundary in op.boundaries]
