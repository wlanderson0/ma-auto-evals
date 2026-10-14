"""Agent-to-agent message interventions as a per-recipient delivery overlay.

Canonical channel messages are never changed (``docs/auditor-tests.md`` A1).
The overlay decides what each recipient receives through Orbit's delivery
seam: a rewritten copy, nothing (dropped), or an auditor-injected message
attributed to a sender. Orbit's receipts record what each recipient was shown,
so exposure evidence follows the overlay (A2). Senders always see their own
messages as sent (A5).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from inspect_ai.util import store_as
from orbit.communication import (
    CommunicationChannel,
    CommunicationDeliveryState,
    CommunicationMessage,
    CommunicationState,
)
from orbit.execution.control import DeliveryRequest

Target = tuple[str, str | None]
"""(message_id, recipient); ``None`` means every recipient."""


@dataclass
class DeliveryOverlay:
    rewrites: dict[Target, str] = field(default_factory=dict)
    drops: set[Target] = field(default_factory=set)
    injections: list[CommunicationMessage] = field(default_factory=list)
    delivered: set[tuple[str, str]] = field(default_factory=set)
    """(recipient, message_id) pairs already returned to a recipient's read."""

    def deliver(self, request: DeliveryRequest) -> list[CommunicationMessage]:
        recipient = request.recipient
        view: list[CommunicationMessage] = []
        for message in request.messages:
            if message.sender == recipient:
                view.append(message)
                continue
            if (message.id, recipient) in self.drops or (message.id, None) in self.drops:
                continue
            content = self.rewrite_for(message.id, recipient)
            if content is not None:
                message = message.model_copy(update={"content": content})
            view.append(message)
        view.extend(m for m in self.injections
                    if m.channel_id == request.channel_id and recipient in m.recipients)
        self.delivered.update((recipient, m.id) for m in view if m.sender != recipient)
        return view

    def rewrite_for(self, message_id: str, recipient: str) -> str | None:
        """What ``recipient`` is shown instead of the message, if it was rewritten for them."""
        content = self.rewrites.get((message_id, recipient))
        return content if content is not None else self.rewrites.get((message_id, None))

    def delivered_to(self, message_id: str) -> set[str]:
        return {recipient for recipient, mid in self.delivered if mid == message_id}


@dataclass(frozen=True)
class CommunicationWorld:
    """Read-only snapshot of a sample's channels, messages and routes."""

    messages: dict[str, CommunicationMessage]
    channels: dict[str, CommunicationChannel]
    permitted_edges: frozenset[tuple[str, str]] | None

    @classmethod
    def current(cls) -> CommunicationWorld:
        state = store_as(CommunicationState)
        edges = store_as(CommunicationDeliveryState).permitted_edges
        return cls(
            messages={m.id: m for m in state.messages},
            channels=dict(state.channels),
            permitted_edges=None if edges is None else frozenset(map(tuple, edges)),
        )

    def readers(self, channel: CommunicationChannel) -> tuple[str, ...]:
        return channel.readers if channel.readers is not None else channel.members

    def writers(self, channel: CommunicationChannel) -> tuple[str, ...]:
        return channel.writers if channel.writers is not None else channel.members

    def route_allowed(self, sender: str, recipient: str) -> bool:
        return self.permitted_edges is None or (sender, recipient) in self.permitted_edges

    def summary(self) -> tuple[dict, ...]:
        return tuple(
            {"id": m.id, "channel_id": m.channel_id, "sender": m.sender,
             "recipients": list(m.recipients), "content": m.content, "sequence": m.sequence}
            for m in sorted(self.messages.values(), key=lambda m: m.sequence)
        )


def recipients_or_all(message: CommunicationMessage, recipients: Sequence[str] | None) -> set[str]:
    return set(message.recipients if recipients is None else recipients)
