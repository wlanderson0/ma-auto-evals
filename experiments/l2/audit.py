"""An L2 audit: its directory, the audit-wide budget ledger and the hard stop
(spec ``docs/level2-interface.md`` §7, §8; plan ``docs/level2-milestone2-mvp.md``).

One directory per audit::

    <audit>/
      brief.md                the brief as given
      ledger.json             spend by kind, reserve, remaining (rewritten on every change)
      launches.jsonl          every launch L2 asked for and what came of it
      experiments/exp-<n>/    one milestone 1 audit directory per launch

Spend is every experiment's ``cost.json`` (milestone 1: each model call in its
logs, agents', helpers' and scorers' alike) plus L2's own calls, which the running
agent reports through ``set_l2_spend`` (its model events, priced like the
experiments'). A reserve (10% of the budget by default) is held back from
experiments for L2's final calls.

**Hard stop.** Once spent plus the reserve reaches the budget, no new experiment
or epoch starts (``stopped``), and the L2 tools refuse everything but ``budget``
and ``end_audit``. An experiment's per-call brief is given what was available for
experiments when it launched as its budget; L2 makes no calls while an experiment
runs, so milestone 1's between-epoch check stops it exactly at the audit-wide
limit. A launched epoch runs to completion, so spending can exceed the budget by
up to one epoch (recorded in ``ledger.json``).
"""

from __future__ import annotations

import dataclasses
import json
import math
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from experiments.l2 import experiment as m1
from experiments.l2.apply import recorded
from experiments.l2.brief import VARIANT_ID, BriefError, _variants, parse_brief
from experiments.l2.cost import log_cost
from experiments.l2.model import L2Brief, Problems, Variant

RESERVE = 0.10
"""Share of the budget held back from experiments for L2's final calls (spec §7)."""
CONDITIONS = ("baseline", "scripted")
"""Inner conditions milestone 2 runs (no inner L0/L1 auditor yet)."""
OVERRUN = ("a launched epoch runs to completion, so spending can exceed the budget by up "
           "to one epoch")
EPSILON = 1e-9
"""Dollars: spend within this of the limit counts as reaching it, so float rounding
never leaves a sliver of budget that would let one more epoch start."""
TEXT_OPS = {"append_line": "persona text", "add_agent": "a new agent's persona"}
"""Ops that write setup text; L2's variants take those texts from the controls."""


class Audit:
    """An L2 audit directory, its ledger, and the gate every experiment goes through."""

    def __init__(self, out: Path, brief: L2Brief, *, mock: bool = False,
                 analysis: Path | None = None, reserve: float = RESERVE,
                 history: Sequence[Path] = (), launch: m1.Launch = m1.launch_subprocess,
                 cost_of: m1.CostOf = log_cost,
                 estimator: Callable[..., dict[str, Any]] = m1.estimate) -> None:
        if not 0 <= reserve < 1:
            raise ValueError(f"reserve must be a share of the budget in [0, 1), not {reserve}")
        self.out = Path(out)
        self.brief = brief
        self.mock = mock
        self.analysis = None if analysis is None else Path(analysis)
        self.reserve = reserve
        self.history = tuple(Path(h) for h in history)
        self.launch_epoch = launch
        self.cost_of = cost_of
        self.estimator = estimator
        self.l2: dict[str, Any] = {"usd": 0.0, "by_model": {}}
        self.ended = False
        self.notes: list[str] = []
        """Problems with the ledger itself (e.g. L2's log could not be priced)."""

    @classmethod
    def create(cls, brief_path: str | Path, out: str | Path, **options: Any) -> Audit:
        """A new audit in ``out`` (new or empty), with a copy of the brief."""
        brief_path, out = Path(brief_path), Path(out)
        brief = parse_brief(brief_path)
        if out.exists() and any(out.iterdir()):
            raise FileExistsError(f"{out} is not empty: each audit gets a directory of its own")
        analysis = options.get("analysis")
        if analysis is not None and not (Path(analysis) / "runs.jsonl").exists():
            raise FileNotFoundError(f"{analysis} is not a milestone 1 audit (no runs.jsonl)")
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(brief_path, out / "brief.md")
        audit = cls(out, brief, **options)
        audit.write_ledger()
        return audit

    # ------------------------------------------------------------ the record
    @property
    def experiments_dir(self) -> Path:
        return self.out / "experiments"

    def controls(self) -> dict[str, Variant]:
        return {c.id: c for c in self.brief.controls or ()}

    def control_of(self, variant: Variant, hint: str | None = None) -> Variant | None:
        """The control ``variant`` is a copy of (with mutations added after the control's,
        and the same inner spec, or the same as a baseline without the opening); ``hint``
        names the one it claims. Without a hint, the longest matching control."""
        def copies(control: Variant) -> bool:
            n = len(control.mutations)
            baseline = dataclasses.replace(control.inner, condition="baseline", opening=())
            return (variant.base == control.base and variant.mutations[:n] == control.mutations
                    and variant.inner in (control.inner, baseline))
        controls = self.controls()
        if hint is not None:
            return controls[hint] if hint in controls and copies(controls[hint]) else None
        found = [c for c in controls.values() if copies(c)]
        return max(found, key=lambda c: len(c.mutations)) if found else None

    def experiments(self) -> list[Path]:
        """Experiment directories, in launch order."""
        if not self.experiments_dir.exists():
            return []
        found = [p for p in self.experiments_dir.iterdir()
                 if p.is_dir() and p.name.startswith("exp-") and p.name[4:].isdigit()]
        return sorted(found, key=lambda p: int(p.name[4:]))

    def sources(self) -> list[Path]:
        """The milestone 1 audit directories results are read from."""
        return [self.analysis] if self.analysis is not None else self.experiments()

    def _append(self, name: str, row: dict[str, Any]) -> None:
        with (self.out / name).open("a") as file:
            file.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    # ---------------------------------------------------------------- ledger
    def experiment_spend(self) -> dict[str, float]:
        spend = {}
        for exp in self.experiments():
            cost = exp / "cost.json"
            spend[exp.name] = (json.loads(cost.read_text())["spent_usd"] if cost.exists()
                               else 0.0)
        return spend

    def set_l2_spend(self, spend: dict[str, Any]) -> None:
        """L2's own spend so far, ``{"usd", "by_model"}`` (``cost.events_cost``). It only
        grows: a lower figure would hand experiments budget L2 already spent. The tools
        refresh it before every call, so a launch always sees the current figure."""
        usd = spend["usd"]
        if isinstance(usd, bool) or not isinstance(usd, int | float) or not math.isfinite(usd) \
                or usd < 0:
            raise ValueError(f"L2's spend must be a finite amount >= 0, not {usd!r}")
        if usd < self.l2["usd"] - EPSILON:
            raise ValueError(f"L2's spend cannot go down (${self.l2['usd']:.6f} -> ${usd:.6f})")
        self.l2 = {"usd": float(max(usd, self.l2["usd"])), "by_model": spend.get("by_model", {})}
        self.write_ledger()

    def budget(self) -> dict[str, Any]:
        """Spent, remaining (budget − ledger total) and the reserve, by kind (spec §7)."""
        experiments = self.experiment_spend()
        spent = sum(experiments.values()) + self.l2["usd"]
        total = self.brief.budget_usd
        reserve = self.reserve * total
        remaining = total - spent
        return {
            "budget_usd": total, "spent_usd": spent, "remaining_usd": remaining,
            "reserve_usd": reserve,
            "available_for_experiments_usd": (0.0 if remaining - reserve <= EPSILON
                                              else remaining - reserve),
            "by_kind": {"experiments": sum(experiments.values()), "l2": self.l2["usd"]},
            "by_experiment": experiments, "l2_by_model": self.l2["by_model"],
            "stopped": self.stop_reason(spent, reserve, total)}

    @staticmethod
    def stop_reason(spent: float, reserve: float, total: float) -> str | None:
        if spent + reserve < total - EPSILON:
            return None
        return (f"the hard stop: spent ${spent:.4f} plus the ${reserve:.4f} reserve reaches "
                f"the ${total:.2f} budget")

    @property
    def stopped(self) -> str | None:
        return self.budget()["stopped"]

    def write_ledger(self, final: bool = False) -> dict[str, Any]:
        ledger = {**self.budget(), "reserve_share": self.reserve, "mock": self.mock,
                  "final": final, "note": OVERRUN, "problems": self.notes}
        (self.out / "ledger.json").write_text(json.dumps(ledger, indent=1, sort_keys=True))
        return ledger

    # ---------------------------------------------------------------- launch
    def used_ids(self) -> dict[str, str]:
        """Variant id (casefolded) -> the experiment that ran it."""
        used = {}
        for exp in self.experiments():
            for row in m1._read_jsonl(exp / "variants.jsonl"):
                used[row["id"].casefold()] = exp.name
        return used

    def check(self, variants: Sequence[Variant],
              controls: Mapping[str, str] | None = None) -> list[tuple[Variant, Any, Problems]]:
        """Each variant built and checked: milestone 1's validity checks, plus what
        milestone 2 and the brief allow. ``controls``: the control each variant id claims
        to start from (otherwise found by matching)."""
        used, seen, checked = self.used_ids(), set(), []
        allowed = self.brief.allowed_changes
        for variant in variants:
            extra = []
            hint = (controls or {}).get(variant.id)
            control = self.control_of(variant, hint)
            if control is None:
                extra.append(
                    f"does not start from a control: it must be a copy of one of the brief's "
                    f"controls {sorted(self.controls())}"
                    + (f" (it names {hint!r})" if hint else "") + " with mutations added")
            added = variant.mutations[len(control.mutations):] if control else ()
            for mutation in added:
                if mutation.op in TEXT_OPS:
                    extra.append(f"{mutation.op} writes {TEXT_OPS[mutation.op]}; in milestone 2 "
                                 "setup texts come only from the controls")
            if not VARIANT_ID.fullmatch(variant.id):
                extra.append(f"id {variant.id!r} may only use letters, digits, '_' and '-'")
            elif variant.id.casefold() in seen:
                extra.append(f"id {variant.id!r} is used twice in this launch")
            elif variant.id.casefold() in used:
                extra.append(f"id {variant.id!r} already ran in {used[variant.id.casefold()]}; "
                             "ids are unique in an audit, so give a new run a new id")
            seen.add(variant.id.casefold())
            if variant.inner.condition not in CONDITIONS:
                extra.append(f"inner condition {variant.inner.condition!r}: milestone 2 runs "
                             f"only {list(CONDITIONS)}")
            if self.brief.base_scenarios is not None and (
                    variant.base not in self.brief.base_scenarios):
                extra.append(f"base {variant.base!r} is not in the brief's base_scenarios "
                             f"{list(self.brief.base_scenarios)}")
            if allowed is not None and (
                    banned := sorted({m.axis for m in added} - set(allowed))):
                extra.append(f"the brief's allowed_changes {list(allowed)} do not allow "
                             f"{banned}")
            try:  # it must survive the per-launch brief that milestone 1 runs from
                [again] = _variants([recorded(variant)], None, None)
                if again != variant:
                    extra.append("the variant does not survive the brief format unchanged")
            except BriefError as error:
                extra.append(f"the variant is not a valid brief variant: {error}")
            _, built, problems = m1.build(variant, self.mock)
            checked.append((variant, built, Problems(
                rejections=(*extra, *problems.rejections), warnings=problems.warnings)))
        return checked

    def launch(self, variants: Sequence[Variant], epochs: int, confirm: bool = False,
               controls: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Check, gate and run one experiment (``experiments/exp-<n>``).

        ``status``: ``refused`` (audit ended, analysis only, hard stop, bad arguments),
        ``rejected`` (a variant failed its checks: nothing ran), ``needs_confirmation``
        (the estimate exceeds what is available for experiments), ``ran`` or
        ``failed`` (an epoch failed; finished epochs are kept). A launch that ran or
        failed uses up its variant ids, even with no epoch done."""
        result: dict[str, Any] = {"ids": [v.id for v in variants], "epochs": epochs,
                                  "confirm": confirm, "experiment": None, "variants": []}
        refused = self._refusal(variants, epochs)
        if refused:
            return self._finish(result, "refused", refused)

        checked = self.check(variants, controls)
        result["variants"] = [
            {"id": v.id, "status": "rejected" if p.rejected else
             ("warned" if p.warnings else "accepted"),
             "rejections": list(p.rejections), "warnings": list(p.warnings)}
            for v, _, p in checked]
        if any(p.rejected for _, _, p in checked):
            return self._finish(result, "rejected",
                                "a variant failed its checks, so nothing ran")

        accepted = [(v, b) for v, b, _ in checked]
        guess = self.estimator(accepted, epochs, [*self.history, *self.experiments()],
                               self.cost_of, self.mock)
        available = self.budget()["available_for_experiments_usd"]
        result.update(estimate=guess, available_usd=available)
        if guess["usd"] > available and not confirm:
            per_epoch = guess["usd"] / epochs
            return self._finish(result, "needs_confirmation", (
                f"the estimate ${guess['usd']:.4f} ({', '.join(guess['basis'])}) exceeds the "
                f"${available:.4f} available for experiments; launch again with "
                "confirm=True to run it anyway, or shrink it (fewer variants or epochs)."
                + (f" Even one epoch (estimated ${per_epoch:.4f}) exceeds it, and a started "
                   "epoch always finishes, so a confirmed launch would overrun the budget by "
                   f"about ${per_epoch - available:.4f}." if per_epoch > available else "")))

        n = max((int(p.name[4:]) for p in self.experiments()), default=0) + 1
        exp = self.experiments_dir / f"exp-{n}"
        result["experiment"] = exp.name
        history = [*self.history, *self.experiments()]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief.md"
            path.write_text(self.experiment_brief(n, variants, available))
            try:
                m1.run_experiment(path, epochs, exp, mock=self.mock, confirm=confirm,
                                  history=history, launch=self.launch_epoch,
                                  cost_of=self.cost_of,
                                  estimator=lambda *args, **kwargs: guess)
                status, reason = "ran", None
            except Exception as error:  # the record keeps the finished epochs
                status, reason = "failed", f"{type(error).__name__}: {error}"[:3000]
        # A failure before milestone 1 wrote its record leaves none of these files.
        decisions = _json(exp / "decisions.json")
        rows = {r["variant"]["id"]: r for r in m1._read_jsonl(exp / "runs.jsonl")}
        for entry in result["variants"]:
            row = rows.get(entry["id"], {})
            entry.update(epochs_done=row.get("epochs_done", 0),
                         cost_usd=row.get("cost_usd", 0.0), errors=row.get("errors", []))
        result["stopped"] = decisions.get("stopped")
        result["spent_usd"] = _json(exp / "cost.json").get("spent_usd", 0.0)
        return self._finish(result, status, reason)

    def record_rejection(self, rows: Sequence[tuple[str, Sequence[str]]], epochs: Any,
                         confirm: bool) -> dict[str, Any]:
        """Record a launch the tools rejected before it became variants (e.g. a JSON
        variant naming no control), as ``launch`` records its own rejections. ``rows``:
        each proposed variant's name and rejections (none for one that passed)."""
        result: dict[str, Any] = {
            "ids": [name for name, _ in rows], "epochs": epochs, "confirm": confirm,
            "experiment": None,
            "variants": [{"id": name, "status": "rejected" if found else "accepted",
                          "rejections": list(found), "warnings": []} for name, found in rows]}
        return self._finish(result, "rejected", "a variant failed its checks, so nothing ran")

    def _refusal(self, variants: Sequence[Variant], epochs: int) -> str | None:
        if self.ended:
            return "the audit has ended"
        if self.analysis is not None:
            return "this is an analysis-only audit: run_experiment is off"
        if stopped := self.stopped:
            return f"{stopped}; no new experiment starts"
        if isinstance(epochs, bool) or not isinstance(epochs, int) or epochs < 1:
            return f"epochs must be a whole number >= 1, not {epochs!r}"
        if not variants:
            return "no variants to run"
        return None

    def _finish(self, result: dict[str, Any], status: str,
                reason: str | None) -> dict[str, Any]:
        result.update(status=status, reason=reason)
        self.write_ledger()
        result["budget"] = self.budget()
        self._append("launches.jsonl", {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **{k: result.get(k) for k in ("status", "reason", "ids", "epochs", "confirm",
                                          "experiment", "available_usd", "stopped",
                                          "spent_usd")},
            "estimate": result.get("estimate"),
            "rejections": [[v["id"], v["rejections"]] for v in result["variants"]
                           if v.get("rejections")],
            "remaining_usd": result["budget"]["remaining_usd"]})
        return result

    def experiment_brief(self, n: int, variants: Sequence[Variant], available: float) -> str:
        """The per-launch brief milestone 1 runs: these variants, and what is available for
        experiments as its budget (so its between-epoch check is the audit's hard stop)."""
        header: dict[str, Any] = {"budget_usd": available}
        if self.brief.mode is not None:
            header["mode"] = self.brief.mode
        header["variants"] = [recorded(v) for v in variants]
        head = yaml.safe_dump(header, sort_keys=False, allow_unicode=True, width=1000)
        return "---\n" + head + "---\n" + (
            f"Experiment {n} of the L2 audit {self.out.name}. budget_usd is what was "
            f"available for experiments at launch (the audit's budget less its spend and "
            f"reserve).\n\nThe audit's question:\n\n{self.brief.question}\n")


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


# --------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> None:
    """``python -m experiments.l2.audit --brief <brief> [--mock] [--fake]
    [--analysis-only <milestone 1 audit dir>]``."""
    import argparse

    from experiments.l2.agent import run_l2
    from experiments.l2.audit_report import write_report
    from experiments.l2.fake import ANALYSIS, FULL, PROMPT, fake_model
    from experiments.l2.tools import Tools

    parser = argparse.ArgumentParser(description="Run an L2 audit (milestone 2).")
    parser.add_argument("--brief", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--mock", action="store_true",
                        help="experiments run on the offline scripted mock models")
    parser.add_argument("--fake", action="store_true",
                        help="L2 is the scripted fake (experiments.l2.fake), not an LLM")
    parser.add_argument("--analysis-only", type=Path, default=None, metavar="AUDIT",
                        help="read this milestone 1 audit; run_experiment is off")
    parser.add_argument("--history", type=Path, nargs="*", default=(),
                        help="earlier milestone 1 audits to estimate costs from")
    args = parser.parse_args(argv)
    if not args.fake:
        raise SystemExit("the LLM L2 agent is step 5 of docs/level2-milestone2-mvp.md and is "
                         "not built yet; use --fake")
    if not args.mock and args.analysis_only is None:
        raise SystemExit("--fake runs experiments live (OpenRouter spend) unless --mock is "
                         "given; live runs wait for review (plan, step 6), so use --mock")
    out = args.out or Path(f"logs/l2-audit/{time.strftime('%Y%m%d-%H%M%S')}"
                           + ("-mock" if args.mock else "") + ("-fake" if args.fake else ""))
    audit = Audit.create(args.brief, out, mock=args.mock, analysis=args.analysis_only,
                         history=args.history)
    tools = Tools(audit, on_end=write_report)
    script = ANALYSIS if args.analysis_only else FULL
    run_l2(tools, fake_model(script), PROMPT)
    budget = audit.budget()
    print(f"spent ${budget['spent_usd']:.4f} of ${budget['budget_usd']:.2f}")
    print(f"report: {out / 'report.md'}")
    if tools.conclusions is None:
        raise SystemExit(2)  # the record is complete, but L2 never ended the audit


if __name__ == "__main__":
    main()
