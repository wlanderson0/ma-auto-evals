"""Figures for L2's write-up (spec §6.6).

``compare_figure`` draws one comparison, as ``scores`` with ``against`` computes it: per
target, the share of runs showing the behaviour on each side, with a 95% Wilson
interval and the k of n written on each bar. It is drawn from the same numbers as the
table L2 reads, so a figure in the write-up is as checkable as that table.

Static PNG (matplotlib, no display), light surface, two categorical slots of the
validated default palette (variant: blue, compared-with: orange), a legend and direct
k/n labels, so identity never rests on colour alone.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FIGURES = "figures"
"""The audit's figure directory (relative to the audit); L2's own figures go there too."""
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SIDES = ("#2a78d6", "#eb6834")  # categorical slots 1 and 2: the variant, then its control


def figure_name(a: str, b: str, scorer: str, field: str | None = None,
                rounds: int | None = None) -> str:
    """The figure's path in the audit, e.g. ``figures/V1-vs-C0-probe.png``."""
    stem = f"{a}-vs-{b}-{scorer}" + (f".{field}" if field else "") + (
        f"-r{rounds}" if rounds is not None else "")
    return f"{FIGURES}/{re.sub(r'[^A-Za-z0-9._-]+', '_', stem)}.png"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% Wilson interval for k of n."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def compare_figure(compared: Mapping[str, Mapping[str, Any]], a: str, b: str, measure: str,
                   path: Path) -> Path:
    """Draw ``compared`` (``Results.compare``) to ``path``."""
    from matplotlib.figure import Figure  # no pyplot: no global backend or state

    targets = list(compared)
    width = 0.36
    fig = Figure(figsize=(max(4.0, 1.6 * len(targets) + 1.5), 3.4), dpi=150)
    ax = fig.subplots()
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for i, (side, vid) in enumerate((("a", a), ("b", b))):
        label: str | None = vid  # on the side's first bar, so the legend always has it
        for j, agent in enumerate(targets):
            cell = compared[agent][side]
            k, n = cell["k"], cell["n"]
            x = j + (i - 0.5) * width
            if n == 0:
                ax.text(x, 0.02, "no runs", ha="center", va="bottom", fontsize=7, color=MUTED)
                continue
            rate, (low, high) = k / n, wilson(k, n)
            # a sliver for 0, so the side's colour still shows next to its label
            ax.bar(x, max(rate, 0.008), width * 0.92, color=SIDES[i], label=label, zorder=2)
            label = None
            ax.errorbar(x, rate, yerr=[[rate - low], [high - rate]], fmt="none",
                        ecolor=INK, elinewidth=1, capsize=3, zorder=3)
            ax.text(x, high + 0.03, f"{k}/{n}", ha="center", va="bottom", fontsize=8,
                    color=INK)
    ax.set_xticks(range(len(targets)), targets, color=INK)
    ax.set_ylim(0, 1.18)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0", "25%", "50%", "75%", "100%"], color=MUTED,
                  fontsize=8)
    ax.set_ylabel("runs showing the behaviour", color=MUTED, fontsize=8)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8, zorder=0)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    ax.set_title(f"{a} vs {b}: {measure}", loc="left", fontsize=10, color=INK, pad=22)
    ax.legend(frameon=False, fontsize=8, loc="lower left", bbox_to_anchor=(0, 1.0), ncols=2,
              borderaxespad=0.2)  # above the plot, under the title: never over a bar
    fig.text(0.01, 0.01, "bars: share of runs (k/n); lines: 95% Wilson interval",
             fontsize=7, color=MUTED)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)
    return path
