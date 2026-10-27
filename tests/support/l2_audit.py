"""Helpers for the milestone 2 audit tests: a milestone 2 brief with a chosen budget,
variants built from its controls, an in-process epoch launcher, fixed costs."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from experiments.l2.brief import parse_brief
from experiments.l2.experiment import run_epoch
from experiments.l2.model import Mutation, Variant

MILESTONE2 = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / "milestone2.md"
DAVE_ON_TOWN = (Mutation("topology", "add_reader", {"agent": "dave", "channel": "town"}),
                Mutation("topology", "add_writer", {"agent": "dave", "channel": "town"}))


def brief(tmp_path: Path, budget: float = 1.0, extra: str = "") -> Path:
    """milestone2.md with ``budget`` (and ``extra`` header lines)."""
    text = MILESTONE2.read_text()
    text, n = re.subn(r"^budget_usd: .*$", f"budget_usd: {budget}{extra}", text, flags=re.M)
    assert n == 1
    path = tmp_path / "brief.md"
    path.write_text(text)
    return path


def variant(brief_path: Path, control: str, vid: str, *mutations: Mutation,
            condition: str | None = None) -> Variant:
    """A copy of ``control`` with ``mutations`` added."""
    base = {c.id: c for c in parse_brief(brief_path).controls}[control]
    inner = base.inner
    if condition is not None:
        inner = dataclasses.replace(inner, condition=condition,
                                    opening=() if condition != "scripted" else inner.opening)
    return dataclasses.replace(base, id=vid, mutations=(*base.mutations, *mutations),
                               inner=inner, rationale=f"test variant {vid}")


class InProcess:
    """Runs each epoch in this process, recording what it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int]] = []

    def __call__(self, brief_path, ids, epoch, out, mock):
        self.calls.append((list(ids), epoch))
        return run_epoch(brief_path, ids, epoch, out, mock)


def fixed(usd: float):
    """A cost function charging ``usd`` per log."""
    return lambda log: {"usd": usd, "samples": [{"usd": usd}]}


def estimate_of(usd: float):
    """An estimator that always says ``usd``."""
    def estimator(accepted, epochs, history, cost_of, mock=False):
        return {"usd": usd, "basis": ["a test figure"], "epochs": epochs,
                "per_variant": {v.id: usd / len(accepted) for v, _ in accepted}}
    return estimator
