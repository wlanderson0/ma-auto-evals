"""The Level 2 data model (spec ``docs/level2-interface.md`` §2–§5).

Plain frozen dataclasses: a brief (``L2Brief``), the variants it fixes or L2
designs (``Variant``, a base scenario plus ``Mutation``s), what runs inside each
variant (``InnerSpec``), what the validity checks found (``Problems``) and what a
run left behind (``VariantRun``: log paths, never measured values).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from orbit_audit.branches import AuditLimits
from orbit_audit.scope import AuditScope

CONDITIONS = ("baseline", "scripted", "l0", "l1")
MODES = ("elicit", "characterise")
AXES = ("topology", "boards", "models", "persona", "schedule", "roster", "task")
"""Kinds of setup change (spec §3.2)."""
MEASURED = ("seat", "targets", "probes", "judge", "mention", "free_probes", "outcome")
"""Changes that make a new scenario rather than a variant (spec §3.2)."""
SCENARIO = "scenario"
"""Writing scenarios (spec §3.5): an ``allowed_changes`` value that is no axis and no
measured change."""
START = "start"


@dataclass(frozen=True)
class Mutation:
    """One change to a scenario's setup (spec §3.1)."""

    axis: str
    """One of ``AXES``."""
    op: str
    """E.g. ``"add_reader"``, ``"set_rounds"``."""
    args: dict[str, Any] = field(default_factory=dict)
    at: str = START
    """``"start"``, or ``"after_round:<n>"`` (schedule changes only, spec §3.3)."""


@dataclass(frozen=True)
class InnerSpec:
    """What runs inside a variant (spec §4)."""

    condition: Literal["baseline", "scripted", "l0", "l1"]
    brief: str | None = None
    """Extra instructions appended to the generated inner goal."""
    opening: tuple[tuple[str, str], ...] = ()
    """``scripted`` only: the seat's fixed opening, as (channel, text) messages."""
    scope: AuditScope | None = None
    """What the inner policy may do, and through whom; ``None``: the default for the
    condition."""
    limits: AuditLimits | None = None
    """Per-sample limits; ``None``: the harness's defaults."""


@dataclass(frozen=True)
class Variant:
    """One setup to test: ``base`` plus ``mutations``, with what runs inside it."""

    id: str
    """Short and unique within an experiment (e.g. ``"M3"``); names the built
    scenario ``<base>--<id>``."""
    base: str
    """The base scenario's key in ``experiments.scenarios.SCENARIOS``."""
    mutations: tuple[Mutation, ...]
    inner: InnerSpec
    rationale: str
    """Why the variant was proposed (required)."""


@dataclass(frozen=True)
class L2Brief:
    """A parsed brief (spec §2): ``None`` means "L2 decides"."""

    question: str
    budget_usd: float
    run_budget_usd: float | None = None
    """A cap on spend on runs (agents, inner policies, scorers), inside ``budget_usd``."""
    mode: Literal["elicit", "characterise"] | None = None
    base_scenarios: tuple[str, ...] | None = None
    base_logs: tuple[str, ...] | None = None
    measure: str | None = None
    allowed_changes: tuple[str, ...] | None = None
    variants: tuple[Variant, ...] | None = None
    controls: tuple[Variant, ...] | None = None
    """Variants L2's own variants can name as a shorthand: such a variant is the
    control's base, mutations and inner, with its own mutations added."""
    hypotheses: tuple[str, ...] | None = None
    inner: InnerSpec | None = None
    """The default inner spec; each variant's own ``inner`` fields override it."""


@dataclass(frozen=True)
class Problems:
    """What the validity checks found (spec §3.4)."""

    rejections: tuple[str, ...] = ()
    """Reasons the variant cannot run; any one means it is never run."""
    warnings: tuple[str, ...] = ()
    """Reasons to doubt the results; the variant runs, and these go with its results."""

    @property
    def rejected(self) -> bool:
        return bool(self.rejections)


@dataclass(frozen=True)
class VariantRun:
    """What running one variant left: problems, spend and logs, never measured values
    (those are read back from the eval logs, spec §6)."""

    variant: Variant
    problems: Problems
    cost_usd: float
    logs: tuple[str, ...]
    """Eval log paths, one per epoch run, in epoch order."""
    epochs_done: int
