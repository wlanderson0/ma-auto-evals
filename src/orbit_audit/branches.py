"""Rollback as forking: branches, recordings, replay and the audit budget.

Orbit never rolls a run back in place ("no pretend rollback"). An audit
rolls back by *forking*: it starts a new, isolated branch of the same sample
that deterministically re-executes the parent up to the fork point, then
continues live with the auditor's interventions (``docs/spec.md`` §5.3).

Every boundary the controller reaches gets a revision number, and the
controller records a ``Step`` per revision: a fingerprint of what it saw, the
operations its policy returned, the generations it served and whether a tool
executed. A child branch re-runs the real Orbit runtime from scratch in a
fresh store, and at each revision before the fork point:

- checks the fingerprint matches the parent's, else raises ``ReplayDesync``;
- re-applies the parent's recorded operations (no policy call);
- serves recorded generations through Orbit's replay seam, so there is no
  provider call, ``ModelEvent`` or spend (R3), while exposure evidence is
  still recorded;
- re-executes tools only if they are declared pure (``ToolReplay.PURE``):
  their effects live in the branch's own store and their results are checked
  through the fingerprint. A fork that would re-execute any other tool is
  rejected before it starts (R5).

At the fork revision the child applies the rollback's interventions, then
continues live under the same policy, which keeps its memory across branches
(like Petri's auditor). Identifiers are deterministic per branch
(``BranchIds``) and, after the fork point, distinct from every other branch.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from inspect_ai.log import transcript
from inspect_ai.model import ModelOutput
from pydantic import BaseModel, ConfigDict

from orbit_audit.boundaries import Boundary

if TYPE_CHECKING:
    from orbit_audit.ops import ControlOp, Rollback
    from orbit_audit.policy import BoundaryView

BRANCH_EVENT_VERSION = 2


# ------------------------------------------------------------------ errors
class BranchStatus(StrEnum):
    """Terminal status of a branch (B2: limits are never a negative finding)."""

    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"
    REPLAY_DESYNC = "replay_desync"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"


class BranchEnded(Exception):  # noqa: N818 - control flow, not a failure kind
    """Ends the current branch; the branch runner classifies it by ``status``."""

    status: BranchStatus = BranchStatus.ERROR


class ReplayDesync(BranchEnded):
    """A branch's replayed prefix diverged from its parent's recording."""

    status = BranchStatus.REPLAY_DESYNC


class RollbackRequested(BranchEnded):
    """The policy asked to fork from an earlier revision; this branch stops."""

    status = BranchStatus.ROLLED_BACK

    def __init__(self, rollback: Rollback) -> None:
        super().__init__(f"rollback to revision {rollback.to_revision}")
        self.rollback = rollback


class BudgetExhausted(BranchEnded):
    """The audit-wide budget is spent; no further live model calls."""

    status = BranchStatus.BUDGET_EXHAUSTED




# ---------------------------------------------------------------- tools
class ToolReplay(StrEnum):
    """How a tool may be re-executed when a branch replays its parent's prefix."""

    PURE = "pure"
    """Effects live only in the sample store and its result depends only on the
    store and arguments: re-executed in the branch, result checked."""
    EFFECTFUL = "effectful"
    """Anything else (sandboxes, network, files): forks that would re-execute it
    are rejected. Undeclared tools are treated as effectful."""


DEFAULT_TOOL_REPLAY: Mapping[str, ToolReplay] = {
    # Orbit's channel tools and submit change only the sample store.
    "channel_post": ToolReplay.PURE,
    "channel_read": ToolReplay.PURE,
    "channel_list": ToolReplay.PURE,
    "submit": ToolReplay.PURE,
}


# ------------------------------------------------------------------ ids
class BranchIds:
    """Orbit identifier provider for one branch.

    Identifiers are a hash of (branch prefix, counter): deterministic,
    so a replayed prefix allocates exactly the parent's identifiers, but with no
    visible structure. Orbit renders message ids into model inputs (inbox
    headers, channel tool results), so an id that encoded the branch index would
    let an agent tell it is on a forked branch. ``diverge`` switches to the
    branch's own prefix, so objects created after the fork never share an
    identifier with another branch's objects.
    """

    def __init__(self) -> None:
        self.count = 0
        self.prefix = 0

    def diverge(self, branch_index: int) -> None:
        self.prefix = branch_index

    def __call__(self) -> str:
        self.count += 1
        return hashlib.blake2b(f"{self.prefix}:{self.count}".encode(), digest_size=16).hexdigest()


# ------------------------------------------------------------- recording
@dataclass
class Step:
    """What the controller saw and did at one revision of a branch."""

    revision: int
    boundary: Boundary
    agent: str | None
    turn: int | None
    fingerprint: str
    label: str
    decisions: tuple[ControlOp, ...] = ()
    outputs: list[ModelOutput] = field(default_factory=list)
    """Generations returned by ``proceed`` at this revision (live or replayed)."""
    tool: str | None = None
    executed: bool = False


@dataclass(frozen=True)
class Replay:
    """Instructions for a child branch: replay ``steps`` up to ``fork_revision``."""

    parent: str
    steps: tuple[Step, ...]
    fork_revision: int
    interventions: tuple[ControlOp, ...] = ()
    reason: str = ""
    """The rollback's reason, recorded on the child branch."""

    def step(self, revision: int) -> Step:
        if revision > len(self.steps):
            raise ReplayDesync(
                f"revision {revision}: the parent {self.parent} recorded only "
                f"{len(self.steps)} boundaries")
        return self.steps[revision - 1]


def fingerprint(view: BoundaryView) -> str:
    """Stable digest of everything a boundary view shows, except its revision.

    Inspect's chat-message IDs are random and never model-visible, so they are
    excluded; Orbit identifiers are deterministic under ``BranchIds``.
    """
    data = {
        "boundary": view.boundary.value, "agent": view.agent, "turn": view.turn,
        "generation": view.generation,
        "input": [m.model_dump(mode="json", exclude={"id"}) for m in view.input],
        "tools": list(view.tools), "tool_name": view.tool_name,
        "tool_call_id": view.tool_call_id, "tool_arguments": view.tool_arguments,
        "tool_result": canonical(view.tool_result),
        "schedule_revision": view.schedule_revision,
        "pending_steps": [list(step) for step in view.pending_steps],
        "messages": list(view.messages),
    }
    encoded = json.dumps(data, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical(value: Any) -> Any:
    """JSON-compatible form of a tool result or other runtime value."""
    if isinstance(value, str | int | float | bool | None):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude={"id"})
    if isinstance(value, Mapping):
        return {str(k): canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [canonical(item) for item in value]
    return str(value)


def label(view: BoundaryView) -> str:
    parts = [view.boundary.value, view.agent or "-"]
    if view.tool_name:
        parts.append(view.tool_name)
    return ":".join(parts)


def fork_problem(steps: tuple[Step, ...], tool_replay: Mapping[str, ToolReplay]) -> str | None:
    """Why a branch with this recording cannot be forked, or ``None`` (R5).

    Any tool not declared pure that executed anywhere in the branch rules out
    forking: replaying it could repeat its side effect, and effects outside
    the sample store (a sandbox, the network) would leak into the child even
    if it ran after the fork point (R6).
    """
    for step in steps:
        if step.executed and tool_replay.get(step.tool or "") is not ToolReplay.PURE:
            return (f"tool {step.tool!r} executed at revision {step.revision} and is not "
                    "declared pure (audit_task(tool_replay=...)); its effects cannot be "
                    "isolated or safely replayed")
    return None


# ---------------------------------------------------------------- budget
class AuditLimits(BaseModel):
    """Audit-wide limits, across every branch of a sample (spec §5.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_branches: int = 8
    max_provider_calls: int | None = None
    """Live target-model calls; replayed generations are not calls."""
    max_tokens: int | None = None


class BudgetLedger:
    """Monotone spend across branches: rollback never refunds (B1)."""

    def __init__(self, limits: AuditLimits) -> None:
        self.limits = limits
        self.provider_calls = 0
        self.tokens = 0
        self.branches = 0

    def exhausted(self) -> str | None:
        limits, calls = self.limits, self.provider_calls
        if limits.max_provider_calls is not None and calls >= limits.max_provider_calls:
            return f"provider call budget spent ({calls}/{limits.max_provider_calls})"
        if limits.max_tokens is not None and self.tokens >= limits.max_tokens:
            return f"token budget spent ({self.tokens}/{limits.max_tokens})"
        return None

    def spend(self, output: ModelOutput) -> None:
        self.provider_calls += 1
        if output.usage is not None:
            self.tokens += output.usage.total_tokens

    def summary(self) -> dict[str, Any]:
        return {"provider_calls": self.provider_calls, "tokens": self.tokens,
                "branches": self.branches, "limits": self.limits.model_dump()}


# ------------------------------------------------------------------ tree
class BranchNode(BaseModel):
    """One branch of an audited sample."""

    model_config = ConfigDict(extra="forbid")

    id: str
    index: int
    parent: str | None = None
    fork_revision: int | None = None
    fork_reason: str = ""
    """The reason given for the rollback that created this branch."""
    interventions: list[dict[str, Any]] = []
    status: BranchStatus | None = None
    reason: str = ""
    revisions: int = 0
    output: str | None = None
    messages: list[dict[str, Any]] = []
    """Canonical channel messages at the branch's end, as ``BoundaryView.messages``
    shows them: abandoned branches are evidence too (spec §3.6)."""


class BranchTree:
    """Append-only record of a sample's branches, rooted at the baseline (R4).

    Nodes are only added, and a node's terminal status is set exactly once.
    Each change is mirrored as an ``orbit_audit_branch`` transcript event.
    """

    def __init__(self) -> None:
        self._nodes: list[BranchNode] = []

    @property
    def nodes(self) -> tuple[BranchNode, ...]:
        return tuple(node.model_copy() for node in self._nodes)

    def add(self, *, parent: str | None = None, fork_revision: int | None = None,
            fork_reason: str = "", interventions: tuple[ControlOp, ...] = ()) -> BranchNode:
        if parent is None and self._nodes:
            raise ValueError("only the baseline branch has no parent")
        if parent is not None and parent not in {node.id for node in self._nodes}:
            raise ValueError(f"unknown parent branch {parent!r}")
        index = len(self._nodes)
        node = BranchNode(
            id=f"branch-{index}", index=index, parent=parent, fork_revision=fork_revision,
            fork_reason=fork_reason,
            interventions=[{"op": op.name, **op.payload()} for op in interventions],
        )
        self._nodes.append(node)
        self._emit("started", node)
        return node

    def finish(self, branch: str, status: BranchStatus, *, reason: str = "",
               revisions: int = 0, output: str | None = None,
               messages: Sequence[dict[str, Any]] = ()) -> None:
        node = next(node for node in self._nodes if node.id == branch)
        if node.status is not None:
            raise ValueError(f"{branch} already finished as {node.status.value}")
        node.status, node.reason, node.revisions, node.output = status, reason, revisions, output
        node.messages = list(messages)
        self._emit("finished", node)

    def _emit(self, action: Literal["started", "finished"], node: BranchNode) -> None:
        transcript().info(
            {"event": "orbit_audit_branch", "version": BRANCH_EVENT_VERSION, "action": action,
             **node.model_dump(mode="json")},
            source="orbit_audit",
        )
