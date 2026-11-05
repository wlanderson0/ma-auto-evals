"""An L2 audit: its directory, the audit-wide budget ledger and the hard stop
(spec ``docs/level2-interface.md`` §7, §8; plan ``docs/level2-milestone2-mvp.md``).

One directory per audit::

    <audit>/
      brief.md                the brief as given
      ledger.json             spend by kind, reserve, remaining (rewritten on every change)
      launches.jsonl          every launch L2 asked for, with its rationale, and what came of it
      experiments/exp-<n>/    one milestone 1 audit directory per launch
      scenarios/              the scenarios L2 wrote: <name>.py and <name>.sha256

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

**Stopping early.** A stop asked for by hand (``interrupt``, Ctrl-C) or the time limit
(``start_clock``; ``TIME_RESERVE`` of it held back for L2's report) is a stop like the
hard stop: ``stopped`` gives its reason, no new experiment starts and the tools refuse
everything but ``budget``, ``request_review`` and ``end_audit``. The time limit also
stops a running experiment before its next epoch; an interrupt lets it finish.

**Run budget.** A brief's ``run_budget_usd`` caps spend on runs inside the budget
(spec §9.4). It stops runs the same way: what is available for experiments is capped
by what is left of it, so the between-epoch check stops at it, and once it is spent
no new experiment starts. The query tools still work, since only runs are capped.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from experiments.l2 import experiment as m1
from experiments.l2 import written as written_
from experiments.l2.apply import recorded
from experiments.l2.brief import VARIANT_ID, BriefError, _variants, parse_brief
from experiments.l2.cost import log_cost
from experiments.l2.model import SCENARIO, L2Brief, Problems, Variant

RESERVE = 0.10
"""Share of the budget held back from experiments for L2's final calls (spec §7)."""
TIME_RESERVE = 0.10
"""Share of the time limit held back for L2's report: once only this is left, the audit
stops like at the hard stop (spec §7)."""
INTERRUPTED = "interrupted by hand (Ctrl-C)"
CONDITIONS = ("baseline", "scripted")
"""Inner conditions milestone 2 runs (no inner L0/L1 auditor yet)."""
OVERRUN = ("a launched epoch runs to completion, so spending can exceed the budget by up "
           "to one epoch")
EPSILON = 1e-9
"""Dollars: spend within this of the limit counts as reaching it, so float rounding
never leaves a sliver of budget that would let one more epoch start."""


class Audit:
    """An L2 audit directory, its ledger, and the gate every experiment goes through."""

    def __init__(self, out: Path, brief: L2Brief, *, mock: bool = False,
                 analysis: Path | None = None, reserve: float = RESERVE,
                 history: Sequence[Path] = (), launch: m1.Launch = m1.launch_subprocess,
                 cost_of: m1.CostOf = log_cost,
                 estimator: Callable[..., dict[str, Any]] = m1.estimate,
                 clock: Callable[[], float] = time.monotonic) -> None:
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
        self.clock = clock
        self.time_limit: float | None = None
        """Seconds for the whole audit (``start_clock``), or ``None``."""
        self.started: float | None = None
        self.interrupted: str | None = None
        """Why the audit was stopped by hand (``interrupt``), if it was."""
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

    @property
    def scenarios_dir(self) -> Path:
        """Where the audit's written scenarios are: its own ``scenarios/``, or, for an
        analysis-only audit, the ``scenarios/`` of the L2 audit holding the analysed
        directory (``<audit>/experiments/exp-<n>``), read only."""
        if self.analysis is None:
            return self.out / "scenarios"
        return self.analysis.parent.parent / "scenarios"

    def written(self) -> written_.WrittenScenarios:
        """The written scenarios, as they are on disk now."""
        return written_.load(self.scenarios_dir)

    @property
    def writes_scenarios(self) -> bool:
        """Whether L2 may write scenarios: not in an analysis-only audit, and only if the
        brief's ``allowed_changes``, when set, names ``scenario``."""
        allowed = self.brief.allowed_changes
        return self.analysis is None and (allowed is None or SCENARIO in allowed)

    def runs_of(self, base: str) -> list[str]:
        """The variants of ``base`` that have run in this audit (at least one epoch)."""
        return [row["variant"]["id"] for exp in self.experiments()
                for row in m1._read_jsonl(exp / "runs.jsonl")
                if row["variant"]["base"] == base and row["epochs_done"]]

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

    def start_clock(self, time_limit: float | None) -> None:
        """Start the audit's clock; ``time_limit`` seconds for the whole audit, if any."""
        self.time_limit, self.started = time_limit, self.clock()

    def time(self) -> dict[str, float | None]:
        """Elapsed and left of the time limit, and the share held back for L2's report."""
        if self.time_limit is None or self.started is None:
            return {"time_limit_s": None, "elapsed_s": None, "time_left_s": None,
                    "time_reserve_s": None}
        elapsed = self.clock() - self.started
        return {"time_limit_s": self.time_limit, "elapsed_s": elapsed,
                "time_left_s": self.time_limit - elapsed,
                "time_reserve_s": TIME_RESERVE * self.time_limit}

    def time_up(self) -> str | None:
        """Why no experiment or epoch may start now for lack of time, if so."""
        t = self.time()
        if t["time_left_s"] is None or t["time_left_s"] > t["time_reserve_s"] + EPSILON:
            return None
        return (f"the time limit: {t['elapsed_s'] / 60:.1f} of {t['time_limit_s'] / 60:.1f} "
                f"min gone, and the last {t['time_reserve_s'] / 60:.1f} min are held back "
                "for L2's report")

    def interrupt(self) -> None:
        """Stop by hand: no new experiment starts (one running finishes), and L2 concludes."""
        self.interrupted = INTERRUPTED

    def budget(self) -> dict[str, Any]:
        """Spent, remaining (budget − ledger total) and the reserve, by kind (spec §7),
        and the time left. With a run budget, what is available for experiments is also
        capped by what is left of it. ``stopped``: the hard stop, the time limit or an
        interrupt, whichever came first in that order."""
        experiments = self.experiment_spend()
        runs = sum(experiments.values())
        spent = runs + self.l2["usd"]
        total = self.brief.budget_usd
        reserve = self.reserve * total
        remaining = total - spent
        available = remaining - reserve
        cap = self.brief.run_budget_usd
        if cap is not None:
            available = min(available, cap - runs)
        return {
            "budget_usd": total, "spent_usd": spent, "remaining_usd": remaining,
            "reserve_usd": reserve,
            "available_for_experiments_usd": 0.0 if available <= EPSILON else available,
            "run_budget_usd": cap, "run_budget_left_usd": None if cap is None else cap - runs,
            "by_kind": {"experiments": sum(experiments.values()), "l2": self.l2["usd"]},
            "by_experiment": experiments, "l2_by_model": self.l2["by_model"],
            **self.time(),
            "stopped": (self.stop_reason(spent, reserve, total) or self.time_up()
                        or self.interrupted)}

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

    def check(self, variants: Sequence[Variant]) -> list[tuple[Variant, Any, Problems]]:
        """Each variant built and checked: milestone 1's validity checks, plus unique ids,
        the conditions L2 runs and the brief's base scenarios (a written scenario is
        always an allowed base). (``allowed_changes`` limits the mutations L2 writes, so
        the tools check it where they read L2's variants.)"""
        used, seen, checked = self.used_ids(), set(), []
        written = self.written()
        for variant in variants:
            extra = []
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
                    variant.base not in self.brief.base_scenarios
                    and variant.base not in written):
                extra.append(f"base {variant.base!r} is not in the brief's base_scenarios "
                             f"{list(self.brief.base_scenarios)}")
            try:  # it must survive the per-launch brief that milestone 1 runs from
                [again] = _variants([recorded(variant)], None, None, written=written)
                if again != variant:
                    extra.append("the variant does not survive the brief format unchanged")
            except BriefError as error:
                extra.append(f"the variant is not a valid brief variant: {error}")
            _, built, problems = m1.build(variant, self.mock, written)
            checked.append((variant, built, Problems(
                rejections=(*extra, *problems.rejections), warnings=problems.warnings)))
        return checked

    def launch(self, variants: Sequence[Variant], epochs: int, confirm: bool = False, *,
               rationale: str | None = None) -> dict[str, Any]:
        """Check, gate and run one experiment (``experiments/exp-<n>``). ``rationale`` is
        required: what the last results showed and why this experiment comes next; it is
        recorded with the launch, whatever came of it.

        ``status``: ``refused`` (audit ended, analysis only, hard stop, bad arguments),
        ``rejected`` (a variant failed its checks: nothing ran), ``needs_confirmation``
        (the estimate exceeds what is available for experiments), ``ran`` or
        ``failed`` (an epoch failed; finished epochs are kept). A launch that ran or
        failed uses up its variant ids, even with no epoch done."""
        result: dict[str, Any] = {"ids": [v.id for v in variants], "epochs": epochs,
                                  "confirm": confirm, "rationale": rationale,
                                  "experiment": None, "variants": []}
        refused = self._refusal(variants, epochs, rationale)
        if refused:
            return self._finish(result, "refused", refused)

        checked = self.check(variants)
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
                               self.cost_of, self.mock, self.written())
        b = self.budget()
        available = b["available_for_experiments_usd"]
        result.update(estimate=guess, available_usd=available)
        if guess["usd"] > available and not confirm:
            per_epoch = guess["usd"] / epochs
            capped = (b["run_budget_left_usd"] is not None
                      and b["run_budget_left_usd"] <= b["remaining_usd"] - b["reserve_usd"])
            return self._finish(result, "needs_confirmation", (
                f"the estimate ${guess['usd']:.4f} ({', '.join(guess['basis'])}) exceeds the "
                f"${available:.4f} available for experiments"
                + (" (what is left of the run budget)" if capped else "")
                + "; launch again with "
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
            path.write_text(self.experiment_brief(n, variants, available, rationale))
            try:
                m1.run_experiment(path, epochs, exp, mock=self.mock, confirm=confirm,
                                  history=history, launch=self.launch_epoch,
                                  cost_of=self.cost_of,
                                  estimator=lambda *args, **kwargs: guess,
                                  stop=self.time_up, scenarios=self.scenarios_dir)
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
                         confirm: bool, *, rationale: Any = None) -> dict[str, Any]:
        """Record a launch the tools rejected before it became variants (e.g. a JSON
        variant naming an unknown control), as ``launch`` records its own rejections. ``rows``:
        each proposed variant's name and rejections (none for one that passed)."""
        result: dict[str, Any] = {
            "ids": [name for name, _ in rows], "epochs": epochs, "confirm": confirm,
            "rationale": rationale, "experiment": None,
            "variants": [{"id": name, "status": "rejected" if found else "accepted",
                          "rejections": list(found), "warnings": []} for name, found in rows]}
        missing = self.rationale_problem(rationale)  # said now, not on the next try
        return self._finish(result, "rejected", "a variant failed its checks, so nothing ran"
                            + (f"; and {missing}" if missing else ""))

    @staticmethod
    def rationale_problem(rationale: Any) -> str | None:
        if isinstance(rationale, str) and rationale.strip():
            return None
        return ("rationale is required: what the last results showed (or, for the first "
                "experiment, what you start from) and why this experiment comes next")

    def _refusal(self, variants: Sequence[Variant], epochs: int,
                 rationale: Any) -> str | None:
        if self.ended:
            return "the audit has ended"
        if self.analysis is not None:
            return "this is an analysis-only audit: run_experiment is off"
        if stopped := self.stopped:
            return f"{stopped}; no new experiment starts"
        b = self.budget()
        if b["run_budget_left_usd"] is not None and b["run_budget_left_usd"] <= EPSILON:
            return (f"the run budget is spent (${b['by_kind']['experiments']:.4f} of "
                    f"${b['run_budget_usd']:.2f} on runs); no new experiment starts, but the "
                    "query tools still work")
        if isinstance(epochs, bool) or not isinstance(epochs, int) or epochs < 1:
            return f"epochs must be a whole number >= 1, not {epochs!r}"
        if not variants:
            return "no variants to run"
        return self.rationale_problem(rationale)

    def _finish(self, result: dict[str, Any], status: str,
                reason: str | None) -> dict[str, Any]:
        result.update(status=status, reason=reason)
        self.write_ledger()
        result["budget"] = self.budget()
        self._append("launches.jsonl", {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **{k: result.get(k) for k in ("status", "reason", "ids", "epochs", "confirm",
                                          "rationale", "experiment", "available_usd",
                                          "stopped", "spent_usd")},
            "estimate": result.get("estimate"),
            "rejections": [[v["id"], v["rejections"]] for v in result["variants"]
                           if v.get("rejections")],
            "remaining_usd": result["budget"]["remaining_usd"]})
        return result

    def experiment_brief(self, n: int, variants: Sequence[Variant], available: float,
                         rationale: str) -> str:
        """The per-launch brief milestone 1 runs: these variants, and what is available for
        experiments as its budget (so its between-epoch check is the audit's hard stop),
        with L2's rationale for the experiment."""
        header: dict[str, Any] = {"budget_usd": available}
        if self.brief.mode is not None:
            header["mode"] = self.brief.mode
        header["variants"] = [recorded(v) for v in variants]
        head = yaml.safe_dump(header, sort_keys=False, allow_unicode=True, width=1000)
        return "---\n" + head + "---\n" + (
            f"Experiment {n} of the L2 audit {self.out.name}. budget_usd is what was "
            f"available for experiments at launch (the audit's budget less its spend and "
            f"reserve, or what was left of its run budget if that was less).\n\n"
            f"The audit's question:\n\n{self.brief.question}\n\n"
            f"Why this experiment (L2's rationale):\n\n{rationale.strip()}\n")


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


# --------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> None:
    """``python -m experiments.l2.audit (--brief <brief> | --question <text> --budget <usd>)
    [--mock] [--fake] [--model M] [--analysis-only <milestone 1 audit dir>]``.

    ``--question`` with ``--budget`` is the shortest brief: the question and the budget,
    everything else left to L2 (spec §1.1); the audit keeps it as its ``brief.md``.

    Without ``--fake``, L2 is an LLM (``--model``, default ``agent.MODEL``) and its own
    calls are live (OpenRouter) even when the experiments run on mocks (``--mock``)."""
    import argparse

    from experiments.l2 import agent
    from experiments.l2.audit_report import write_report
    from experiments.l2.fake import ANALYSIS, FULL, PROMPT, fake_model
    from experiments.l2.tools import Tools

    parser = argparse.ArgumentParser(description="Run an L2 audit (milestone 2).")
    given = parser.add_mutually_exclusive_group(required=True)
    given.add_argument("--brief", type=Path)
    given.add_argument("--question", help="a plain-text brief: the question alone, with "
                                          "--budget; L2 decides everything else")
    parser.add_argument("--budget", type=float, default=None, metavar="USD",
                        help="with --question: the budget, in dollars")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--mock", action="store_true",
                        help="experiments run on the offline scripted mock models")
    parser.add_argument("--fake", action="store_true",
                        help="L2 is the scripted fake (experiments.l2.fake), not an LLM")
    parser.add_argument("--model", default=None,
                        help="L2's model (default: experiments.l2.agent.MODEL)")
    parser.add_argument("--time-limit", type=float, default=None, metavar="SECONDS",
                        help="wall-clock time for the whole audit, overriding the brief's "
                             "time_limit: once only the last 10%% is left, no new experiment "
                             "or epoch starts and L2 writes its report")
    parser.add_argument("--live", action="store_true",
                        help="allow OpenRouter calls (an LLM L2, or experiments without "
                             "--mock); without it, anything that would spend money stops")
    parser.add_argument("--analysis-only", type=Path, default=None, metavar="AUDIT",
                        help="read this milestone 1 audit; run_experiment is off")
    parser.add_argument("--share", action="store_true",
                        help="when the audit ends, upload its experiments to Cosmos and link "
                             "the report to them (experiments.l2.share)")
    parser.add_argument("--history", type=Path, nargs="*", default=(),
                        help="earlier milestone 1 audits to estimate costs from")
    args = parser.parse_args(argv)
    if (args.question is None) != (args.budget is None):
        parser.error("--question and --budget go together (a --brief sets its own budget)")
    if args.fake and args.model:
        raise SystemExit("--fake and --model exclude each other")
    if args.fake and not args.mock and args.analysis_only is None:
        raise SystemExit("--fake runs experiments live (OpenRouter spend) unless --mock is "
                         "given; a scripted L2 has no use for live runs, so use --mock")
    if not args.fake:
        from experiments.l2.cost import UnknownPrice, price

        try:
            price(args.model or agent.MODEL)
        except UnknownPrice as error:  # L2 could not count its own spend
            raise SystemExit(str(error)) from error
    live_l2 = not args.fake and not (args.model or "").startswith("mockllm/")
    live_runs = not args.mock and args.analysis_only is None
    if live_l2 or live_runs:
        what = " and ".join(x for x, on in (("L2's own calls", live_l2),
                                             ("the experiments", live_runs)) if on)
        if not args.live:
            raise SystemExit(f"{what} would call OpenRouter and spend money: add --live to "
                             "allow it (budget: the brief's budget_usd)")
        import os

        from dotenv import load_dotenv

        load_dotenv(m1.REPO / ".env")  # OPENROUTER_API_KEY
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise SystemExit(f"{what} need OPENROUTER_API_KEY (environment or .env)")
    out = args.out or Path(f"logs/l2-audit/{time.strftime('%Y%m%d-%H%M%S')}"
                           + ("-analysis" if args.analysis_only else "")
                           + ("-mock" if args.mock else "") + ("-fake" if args.fake else ""))
    with tempfile.TemporaryDirectory() as tmp:
        brief_path = args.brief
        if args.question is not None:
            brief_path = Path(tmp) / "brief.md"
            brief_path.write_text(f"---\nbudget_usd: {args.budget}\n---\n"
                                  f"{args.question.strip()}\n")
        audit = Audit.create(brief_path, out, mock=args.mock, analysis=args.analysis_only,
                             history=args.history)
    tools = Tools(audit, on_end=write_report)
    try:
        if args.fake:
            script = ANALYSIS if args.analysis_only else FULL
            agent.run_l2(tools, fake_model(script), PROMPT, time_limit=args.time_limit)
        else:
            model = args.model or agent.MODEL
            print(f"L2 is {model}; budget ${audit.brief.budget_usd:.2f} (L2's calls "
                  f"included); audit: {out}. Ctrl-C once: the running experiment finishes, "
                  "then L2 concludes; twice: stop at once.", flush=True)
            agent.run_l2(tools, model, agent.SYSTEM_PROMPT, time_limit=args.time_limit)
    except KeyboardInterrupt:  # a second Ctrl-C: the record is written all the same
        print(f"stopped by hand; report: {out / 'report.md'}")
        raise SystemExit(130) from None
    budget = audit.budget()
    print(f"spent ${budget['spent_usd']:.4f} of ${budget['budget_usd']:.2f}")
    print(f"report: {out / 'report.md'}")
    if tools.conclusions is None:
        if args.share:
            print("not shared on Cosmos: L2 never ended the audit")
        raise SystemExit(2)  # the record is complete, but L2 never ended the audit
    if args.share:
        from experiments.l2.share import share_or_explain

        if not share_or_explain(out):  # the audit is recorded either way
            raise SystemExit(3)


if __name__ == "__main__":
    main()
