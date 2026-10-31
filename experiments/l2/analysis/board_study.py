"""The board study's analysis (plan ``docs/level2-milestone6.md``, scope items 3 and 5).

Usage::

    uv run python -m experiments.l2.analysis.board_study logs/l2/board-study-grid \\
        [--calibration logs/l2/board-calibration-2/batch-*]

Reads only the eval logs (through ``experiments.l2.results.Results`` and the logs' own
stores) and prints the write-up's tables as Markdown. The measure, the comparisons and
their correction are the ones the grid brief fixed before any run
(``experiments/l2/briefs/board-study-grid.md``):

- per target (Bob, Carol, Dave), the runs with at least one use of the workaround
  (the outcome's ``uses`` > 0), k of n, B1 (the post) against B0 (a neutral post);
- primary: plain rule, per model, per target, Fisher exact (``tools.fisher_p``, the test
  ``compare`` reports), Holm-corrected over the 9 at family-wise 0.05; only these can be
  findings. For each, the smallest B1 count that would have reached p < 0.05 given its
  observed B0 count;
- secondary, leads only: the same with the soft rule (raw p), and whether the post's
  effect differs between models (soft rule) or between rules (per model), by a
  likelihood-ratio test of the interaction term in a logistic regression, per target. A
  regression with a 0 of n or n of n cell has no finite fit; it is reported as such;
- alongside, not evidence: reposting (runs with at least one post the judge flagged),
  and the seat's own uses in the B0 arms (the off-dose check), per model; the m1/plain
  cells against milestone 5's calibration (``--calibration``: its experiment directories,
  pooled; variants ``B1`` and ``B0``).
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from inspect_ai.log import read_eval_log
from inspect_ai.util import Store

from experiments.l2.results import Results
from experiments.l2.tools import fisher_p
from experiments.scenarios import replica_outcome

MODELS = {"m1": "gpt-4.1-mini", "m2": "GLM 5.2", "m3": "GLM 5.3"}
RULES = ("plain", "soft")
ALPHA = 0.05


# ------------------------------------------------------------------- statistics
def holm(ps: Sequence[float]) -> list[float]:
    """Holm-adjusted p-values, in the order given."""
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adjusted, running = [0.0] * len(ps), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(ps) - rank) * ps[i]))
        adjusted[i] = running
    return adjusted


def smallest_detectable(k0: int, n0: int, n1: int, alpha: float = ALPHA) -> int | None:
    """The smallest B1 count above ``k0`` (of ``n1``) whose Fisher p is below ``alpha``."""
    return next((k for k in range(k0 + 1, n1 + 1) if fisher_p(k, n1, k0, n0) < alpha), None)


def _fit(x: np.ndarray, y: np.ndarray) -> float:
    """The maximised log-likelihood of a logistic regression (Newton's method)."""
    beta = np.zeros(x.shape[1])
    for _ in range(100):
        p = 1 / (1 + np.exp(-x @ beta))
        step = np.linalg.solve(x.T @ (x * (p * (1 - p))[:, None]), x.T @ (y - p))
        beta += step
        if np.max(np.abs(step)) < 1e-10:
            break
    p = np.clip(1 / (1 + np.exp(-x @ beta)), 1e-12, 1 - 1e-12)
    return float(np.sum(y * np.log(p) + (1 - y) * np.log(1 - p)))


def chi2_sf(stat: float, df: int) -> float:
    """The chi-squared upper tail, for 1 or 2 degrees of freedom."""
    if df == 1:
        return math.erfc(math.sqrt(max(stat, 0.0) / 2))
    if df == 2:
        return math.exp(-max(stat, 0.0) / 2)
    raise ValueError("df must be 1 or 2")


def interaction(cells: dict[tuple[str, int], tuple[int, int]]) -> tuple[float, float] | None:
    """Likelihood-ratio test that the post's effect is the same in every group.

    ``cells``: ``(group, post) -> (k, n)`` with post 1 (B1) or 0 (B0). The full model has
    a group effect, a post effect and their interaction; the reduced one drops the
    interaction. ``(statistic, p)``, or ``None`` when a cell is 0 of n or n of n (no
    finite fit)."""
    if any(k in (0, n) for k, n in cells.values()):
        return None
    groups = sorted({g for g, _ in cells})
    rows, ys = [], []
    for (group, post), (k, n) in cells.items():
        dummies = [float(group == g) for g in groups[1:]]
        row = [1.0, float(post), *dummies, *(post * d for d in dummies)]
        rows += [row] * n
        ys += [1.0] * k + [0.0] * (n - k)
    x, y = np.array(rows), np.array(ys)
    reduced = x[:, :2 + len(groups) - 1]
    stat = 2 * (_fit(x, y) - _fit(reduced, y))
    return stat, chi2_sf(stat, len(groups) - 1)


# ------------------------------------------------------------------- the logs
def kn(results: Results, vid: str, agent: str, scorer: str = "outcome") -> tuple[int, int]:
    """k of n for ``agent`` in ``vid``: runs with a use of the workaround (``outcome``) or
    with a post the judge flagged (``post_judge``)."""
    if scorer == "outcome":
        known = [results.adopted(s, agent, "outcome", field="uses") for s in results.of(vid)]
    else:
        known = [None if (v := (s.scores.get("post_judge") or {}).get("agents", {})
                              .get(agent, {}).get("flagged_posts")) is None else v > 0
                 for s in results.of(vid)]
    done = [x for x in known if x is not None]
    return sum(done), len(done)


def seat_uses(results: Results, vid: str, seat: str = "alice") -> tuple[int, int]:
    """Runs of ``vid`` in which the seat itself used the workaround, from each log's store."""
    used = [replica_outcome(Store(read_eval_log(s.log).samples[0].store), seat, None)["uses"]
            for s in results.of(vid)]
    return sum(u > 0 for u in used), len(used)


# ------------------------------------------------------------------- report
def load(experiment: Path) -> Results:
    results = Results.load(experiment)
    if results.problems or not results.samples:
        raise ValueError(f"{experiment}: " + ("; ".join(results.problems) or "no samples"))
    return results


def report(grid: Path, calibration: Sequence[Path] = ()) -> str:
    results = load(grid)
    targets = results.targets("m1-plain-B1")
    lines = ["### Use of the workaround, B1 (post) vs B0 (neutral post), k of n", "",
             "| model | rule | " + " | ".join(targets) + " |",
             "|---|---|" + "---|" * len(targets)]
    cell = {}
    for m in MODELS:
        for rule in RULES:
            row = []
            for agent in targets:
                b1, b0 = (kn(results, f"{m}-{rule}-{arm}", agent) for arm in ("B1", "B0"))
                cell[m, rule, agent] = (b1, b0)
                row.append(f"{b1[0]}/{b1[1]} vs {b0[0]}/{b0[1]}")
            lines.append(f"| {MODELS[m]} | {rule} | " + " | ".join(row) + " |")

    primary = [(m, a) for m in MODELS for a in targets]
    raw = [fisher_p(*cell[m, "plain", a][0], *cell[m, "plain", a][1]) for m, a in primary]
    lines += ["", "### Primary: plain rule, B1 vs B0 (Fisher exact; Holm over the 9)", "",
              "| model | target | B1 | B0 | p | Holm p | smallest B1 for p < 0.05 |",
              "|---|---|---|---|---|---|---|"]
    for (m, a), p, adj in zip(primary, raw, holm(raw), strict=True):
        (k1, n1), (k0, n0) = cell[m, "plain", a]
        need = smallest_detectable(k0, n0, n1)
        lines.append(f"| {MODELS[m]} | {a} | {k1}/{n1} | {k0}/{n0} | {p:.3g} | {adj:.3g} | "
                     f"{'none' if need is None else f'{need}/{n1}'} |")

    lines += ["", "### Secondary (leads): soft rule, B1 vs B0 (Fisher exact, raw p)", "",
              "| model | target | B1 | B0 | p |", "|---|---|---|---|---|"]
    for m in MODELS:
        for a in targets:
            (k1, n1), (k0, n0) = cell[m, "soft", a]
            lines.append(f"| {MODELS[m]} | {a} | {k1}/{n1} | {k0}/{n0} | "
                         f"{fisher_p(k1, n1, k0, n0):.3g} |")

    lines += ["", "### Secondary (leads): does the post's effect differ? "
                  "(logistic regression, likelihood-ratio test of the interaction)", "",
              "| comparison | target | LR statistic | p |", "|---|---|---|---|"]
    tests = []
    for a in targets:
        tests.append(("post x model, soft rule", a,
                      {(m, post): cell[m, "soft", a][1 - post] for m in MODELS for post in (1, 0)}))
        tests.append(("post x model, plain rule", a,
                      {(m, post): cell[m, "plain", a][1 - post] for m in MODELS
                       for post in (1, 0)}))
        for m in MODELS:
            tests.append((f"post x rule, {MODELS[m]}", a,
                          {(rule, post): cell[m, rule, a][1 - post] for rule in RULES
                           for post in (1, 0)}))
    for name, a, cells in tests:
        found = interaction(cells)
        lines.append(f"| {name} | {a} | " + ("no finite fit (a 0 of n or n of n cell) | – |"
                                             if found is None else
                                             f"{found[0]:.2f} | {found[1]:.3g} |"))

    lines += ["", "### Alongside, not evidence", "",
              "Reposting: runs with at least one post the judge flagged (B1 vs B0).", "",
              "| model | rule | " + " | ".join(targets) + " |",
              "|---|---|" + "---|" * len(targets)]
    for m in MODELS:
        for rule in RULES:
            row = []
            for a in targets:
                b1, b0 = (kn(results, f"{m}-{rule}-{arm}", a, "post_judge")
                          for arm in ("B1", "B0"))
                row.append(f"{b1[0]}/{b1[1]} vs {b0[0]}/{b0[1]}")
            lines.append(f"| {MODELS[m]} | {rule} | " + " | ".join(row) + " |")
    lines += ["", "The seat's own uses in the B0 arms (off-dose; flagged, never removed).", "",
              "| model | plain | soft |", "|---|---|---|"]
    for m in MODELS:
        plain, soft = (seat_uses(results, f"{m}-{rule}-B0") for rule in RULES)
        lines.append(f"| {MODELS[m]} | {plain[0]}/{plain[1]} | {soft[0]}/{soft[1]} |")

    if calibration:
        earlier = [load(path) for path in calibration]
        lines += ["", f"Milestone 5 check: {MODELS['m1']}, plain rule, this grid vs the "
                      f"calibration ({len(earlier)} experiment(s), pooled; Fisher exact per "
                      "arm).", "",
                  "| target | arm | grid | milestone 5 | p |", "|---|---|---|---|---|"]
        for a in targets:
            for arm, side in (("B1", 0), ("B0", 1)):
                counts = [kn(r, arm, a) for r in earlier]
                now, then = cell["m1", "plain", a][side], tuple(map(sum, zip(*counts, strict=True)))
                lines.append(f"| {a} | {arm} | {now[0]}/{now[1]} | {then[0]}/{then[1]} | "
                             f"{fisher_p(*now, *then):.3g} |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("grid", type=Path)
    parser.add_argument("--calibration", type=Path, nargs="+", default=())
    args = parser.parse_args(argv)
    print(report(args.grid, args.calibration), end="")


if __name__ == "__main__":
    main()
