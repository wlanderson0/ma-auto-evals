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
experiments'). A reserve is held back from experiments for L2's final calls: 10% of
the budget by default, or ``CLOSING_TURNS`` of L2's calls at their current price if
that is more (``turn_usd``), so a long audit on a dear model can still pay for its
review and its end.

**Hard stop.** Once spent plus the reserve reaches the budget, no new experiment
or epoch starts (``stopped``), and the L2 tools refuse everything but ``budget``,
``request_review`` and ``end_audit``. An experiment's per-call brief is given what was
available for experiments when it launched as its budget; L2 makes no calls while an
experiment runs, so milestone 1's between-epoch check stops it exactly at the
audit-wide limit. A launched epoch runs to completion, so spending can exceed the budget by
up to one epoch (recorded in ``ledger.json``). Once the budget is spent, L2 still gets
``agent.CLOSING_OVERRUN`` turns to review and end the audit (only those tools run), so
it can go over by those few calls too; if it still has not ended, the harness ends it
with the draft L2 sent for review (``agent.finalize``).

**Stopping early.** A stop asked for by hand (``interrupt``, Ctrl-C) or by the time
limit (``start_clock``; ``TIME_RESERVE`` of it held back for L2's report) stops runs as
the run budget does: ``halted`` gives its reason, no new experiment starts, and L2 is
told to conclude; the query tools still work, so it can read what ran. The time limit,
a second interrupt, or a stop request through the mailbox (read at each epoch's end)
also stops a running experiment before its next epoch; a first Ctrl-C lets it finish.

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
from typing import TYPE_CHECKING, Any

import yaml

from experiments.l2 import experiment as m1
from experiments.l2 import written as written_
from experiments.l2.apply import recorded
from experiments.l2.brief import VARIANT_ID, BriefError, _variants, parse_brief
from experiments.l2.cost import log_cost
from experiments.l2.figures import FIGURES
from experiments.l2.model import SCENARIO, L2Brief, Problems, Variant
from experiments.l2.sandbox import Sandbox

if TYPE_CHECKING:
    from experiments.l2.steer import Steering

RESERVE = 0.10
"""Share of the budget held back from experiments for L2's final calls, at least (spec §7;
``CLOSING_TURNS`` can make the reserve larger)."""
CLOSING_TURNS = 5
"""Model calls the reserve must pay for once experiments stop: L2's request_review turn,
two of the reviewer's, L2's end_audit turn and one spare. Each is priced at the dearest
of L2's latest calls (``turn_usd``), since L2's context, and so its cost per call, grows
through the audit."""
TIME_RESERVE = 0.10
"""Share of the time limit held back for L2's report: once only this is left, no new
experiment or epoch starts (spec §7)."""
CONDITIONS = ("baseline", "scripted")
"""Inner conditions milestone 2 runs (no inner L0/L1 auditor yet)."""
OVERRUN = ("a launched epoch runs to completion, so spending can exceed the budget by up "
           "to one epoch; and once the budget is spent, L2 still gets a few closing turns "
           "(only request_review and end_audit run) so the audit ends with a report")
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
                 clock: Callable[[], float] = time.monotonic,
                 progress: Callable[[dict[str, Any]], None] | None = None,
                 sandbox: Sandbox | None = None) -> None:
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
        self.recent_calls: list[float] = []
        """What L2's latest model calls cost (the reviewer's included), newest last; the
        reserve is sized from them (``turn_usd``)."""
        self.ended = False
        self.clock = clock
        self.time_limit: float | None = None
        """Seconds for the whole audit (``start_clock``), or ``None``."""
        self.started: float | None = None
        self.interrupts = 0
        """How many times the audit was interrupted by hand (``interrupt``)."""
        self.interrupt_reason: str | None = None
        """Why the first interrupt came, when it was not Ctrl-C (a stop request)."""
        self.paused_since: float | None = None
        """When the audit was paused (``pause``), while it is."""
        self.paused_s = 0.0
        """Seconds the audit spent paused before the current pause; its clock stops then."""
        self.budget_usd = brief.budget_usd
        """The audit's total now: the brief's budget, or what a budget request raised it to
        (``raise_budget``)."""
        self.raises: list[dict[str, Any]] = []
        """Each raise of the total: from, to, by whom, at which step of L2's, when."""
        self.steering: Steering | None = None
        """The mailbox reader when the audit is steered (``experiments.l2.steer``)."""
        self.launching = False
        """Whether a launch is running (its record is still to be written)."""
        self.notes: list[str] = []
        """Problems with the ledger itself (e.g. L2's log could not be priced)."""
        self.progress = progress
        """Called with each progress event (``say``); the CLI shows them
        (``experiments.l2.progress``). None: silent."""
        self.sandbox = sandbox
        """Where L2's code runs (``run_python``, the check of a written scenario), or
        ``None``: outside any sandbox, as before (``experiments.l2.sandbox``)."""

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

    @property
    def holds_scenarios(self) -> bool:
        """Whether a written scenario is on disk for this audit to import (looked for,
        not imported): its own, or, for an analysis-only audit, the analysed audit's."""
        return self.scenarios_dir.is_dir() and any(self.scenarios_dir.glob("*.py"))

    def sandbox_report(self) -> dict[str, Any] | None:
        """The sandbox this audit may report (Cosmos spec 22 §5, ``program.json``):
        ``{"kind", "network": False, "write"}`` only when it has one, writes no scenarios
        and holds none, since a written scenario is imported into this process and run in
        each epoch's subprocess, both outside the sandbox. ``None`` otherwise, and then
        text from Cosmos never reaches L2 (rule R18; ``experiments.l2.steer``)."""
        if self.sandbox is None or self.writes_scenarios or self.holds_scenarios:
            return None
        return {"kind": self.sandbox.kind, "network": False,
                "write": str((self.out / FIGURES).resolve())}

    def runs_of(self, base: str) -> list[str]:
        """The variants of ``base`` that have run in this audit (at least one epoch)."""
        return [row["variant"]["id"] for exp in self.experiments()
                for row in m1._read_jsonl(exp / "runs.jsonl")
                if row["variant"]["base"] == base and row["epochs_done"]]

    def _append(self, name: str, row: dict[str, Any]) -> None:
        with (self.out / name).open("a") as file:
            file.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    def say(self, event: str, **fields: Any) -> None:
        """A progress event, ``{"event", **fields, "budget"}`` with the budget as it now
        stands: ``tool`` (a call starts or ends), ``launch``, ``epoch`` (starts or
        ends) and ``spend`` (L2's own spend moved). Nothing is recorded."""
        if self.progress is not None:
            self.progress({"event": event, **fields, "budget": self.budget()})

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
        before = self.l2["usd"]
        self.l2 = {"usd": float(max(usd, self.l2["usd"])), "by_model": spend.get("by_model", {})}
        if self.l2["usd"] > before + EPSILON:
            self.recent_calls = [*self.recent_calls, self.l2["usd"] - before][-3:]
        self.write_ledger()
        if self.l2["usd"] > before:
            self.say("spend")

    def start_clock(self, time_limit: float | None) -> None:
        """Start the audit's clock; ``time_limit`` seconds for the whole audit, if any."""
        self.time_limit, self.started = time_limit, self.clock()

    def clock_state(self) -> dict[str, float | None]:
        """Elapsed and left of the time limit, and the share held back for L2's report. The
        clock does not run while the audit is paused."""
        elapsed = None if self.started is None else self.clock() - self.started - self.paused()
        if self.time_limit is None or elapsed is None:
            return {"time_limit_s": None, "elapsed_s": elapsed, "time_left_s": None,
                    "time_reserve_s": None}
        return {"time_limit_s": self.time_limit, "elapsed_s": elapsed,
                "time_left_s": self.time_limit - elapsed,
                "time_reserve_s": TIME_RESERVE * self.time_limit}

    def time_up(self) -> str | None:
        """Why no experiment or epoch may start now for lack of time, if so."""
        t = self.clock_state()
        if t["time_left_s"] is None or t["time_left_s"] > t["time_reserve_s"] + EPSILON:
            return None
        return (f"the time limit: {t['elapsed_s'] / 60:.1f} of {t['time_limit_s'] / 60:.1f} "
                f"min gone, and the last {t['time_reserve_s'] / 60:.1f} min are held back "
                "for L2's report")

    def interrupt(self, reason: str | None = None) -> None:
        """Stop by hand: no new experiment starts, and L2 concludes. A first interrupt lets
        a running experiment finish; a second stops it before its next epoch. ``reason``
        says why, when it was not Ctrl-C: a researcher's stop request, through the mailbox.
        Such a stop also ends a running experiment before its next epoch (``epoch_stop``),
        since the person who sent it may be nowhere near the terminal; a second request
        changes nothing, and a Ctrl-C after it counts as the second interrupt."""
        if reason is None:
            self.interrupts += 1
        elif self.interrupt_reason is None:
            self.interrupt_reason = reason
            self.interrupts = max(self.interrupts, 1)

    @property
    def interrupted(self) -> str | None:
        return (None if not self.interrupts else
                self.interrupt_reason or "interrupted by hand (Ctrl-C)"
                if self.interrupts == 1 else "stopped by hand (Ctrl-C twice)")

    def pause(self) -> None:
        """Hold new launches (a pause request): the audit's clock stops until ``resume``."""
        if self.paused_since is None:
            self.paused_since = self.clock()
            self.write_ledger()

    def resume(self) -> None:
        if self.paused_since is not None:
            self.paused_s += self.clock() - self.paused_since
            self.paused_since = None
            self.write_ledger()

    def paused(self) -> float:
        """Seconds the audit has been paused, the pause running now included."""
        now = 0.0 if self.paused_since is None else self.clock() - self.paused_since
        return self.paused_s + now

    def raise_budget(self, usd: float, *, by: str, at_step: int) -> None:
        """Raise the audit's total to ``usd`` (a budget request): from the next experiment
        on, as one that is running keeps the budget it was launched with."""
        if not usd > self.budget_usd:
            raise ValueError(f"${usd} is not above the total ${self.budget_usd}")
        self.raises.append({"from": self.budget_usd, "to": usd, "by": by, "at_step": at_step,
                            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        self.budget_usd = usd
        self.write_ledger()

    @property
    def halted(self) -> str | None:
        """Why no experiment may start now, other than money: the time limit or an
        interrupt."""
        return self.time_up() or self.interrupted

    def epoch_stop(self) -> str | None:
        """Why a running experiment stops before its next epoch: the time limit, a second
        interrupt, or a researcher's stop request (the mailbox is read at each epoch's
        end)."""
        asked = self.interrupts > 1 or self.interrupt_reason is not None
        return self.time_up() or (self.interrupted if asked else None)

    def budget(self) -> dict[str, Any]:
        """Spent, remaining (budget − ledger total) and the reserve, by kind (spec §7),
        and the time left. With a run budget, what is available for experiments is also
        capped by what is left of it. ``stopped``: the hard stop; ``halted``: a stop by
        time or by hand."""
        experiments = self.experiment_spend()
        runs = sum(experiments.values())
        spent = runs + self.l2["usd"]
        total = self.budget_usd
        reserve = max(self.reserve * total, CLOSING_TURNS * self.turn_usd)
        remaining = total - spent
        available = remaining - reserve
        cap = self.brief.run_budget_usd
        if cap is not None:
            available = min(available, cap - runs)
        return {
            "budget_usd": total, "spent_usd": spent, "remaining_usd": remaining,
            "reserve_usd": reserve, "l2_turn_usd": self.turn_usd,
            "available_for_experiments_usd": 0.0 if available <= EPSILON else available,
            "run_budget_usd": cap, "run_budget_left_usd": None if cap is None else cap - runs,
            "by_kind": {"experiments": sum(experiments.values()), "l2": self.l2["usd"]},
            "by_experiment": experiments, "l2_by_model": self.l2["by_model"],
            **self.clock_state(), "halted": self.halted,
            "stopped": self.stop_reason(spent, reserve, total),
            **({"raised": list(self.raises)} if self.raises else {})}

    @property
    def turn_usd(self) -> float:
        """What one of L2's model calls costs now: the dearest of its latest three (a
        figure L2 reports per tool call can cover more than one model call, so this errs
        high)."""
        return max(self.recent_calls, default=0.0)

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
        if self.steering is not None:
            ledger["steering"] = self.steering.summary()
        if self.sandbox is not None:
            ledger["sandbox"] = {"kind": self.sandbox.kind, "reported": self.sandbox_report()}
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
        failed uses up its variant ids, even with no epoch done.

        A steered audit looks at its mailbox first, and waits while it is paused, so a
        stop or a raise that comes meanwhile is seen by the checks. It looks again at the
        end of each epoch and before the next: a pause holds the next epoch, and a stop
        ends the experiment there."""
        self.launching = True
        try:
            if self.steering is not None:
                self.steering.hold()
            return self._launch(variants, epochs, confirm, rationale)
        finally:
            self.launching = False

    def _launch(self, variants: Sequence[Variant], epochs: int, confirm: bool,
                rationale: str | None) -> dict[str, Any]:
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
        self.say("launch", experiment=exp.name, variants=[v.id for v in variants],
                 epochs=epochs, estimate_usd=guess["usd"], available_usd=available)

        def launch_epoch(*args: Any) -> Any:
            epoch, started = args[2], time.monotonic()
            self.say("epoch", experiment=exp.name, epoch=epoch, epochs=epochs, done=False)
            try:
                paths = self.launch_epoch(*args)
            finally:  # a request sent during a long epoch is read before it expires
                if self.steering is not None:
                    self.steering.poll()
            self.say("epoch", experiment=exp.name, epoch=epoch, epochs=epochs, done=True,
                     seconds=time.monotonic() - started)
            return paths

        def before_epoch() -> str | None:
            """Why the next epoch does not start. A steered audit looks at its mailbox
            first and waits while it is paused, as before a launch."""
            if self.steering is not None:
                self.steering.hold()
            return self.epoch_stop()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief.md"
            path.write_text(self.experiment_brief(n, variants, available, rationale))
            try:
                m1.run_experiment(path, epochs, exp, mock=self.mock, confirm=confirm,
                                  history=history, launch=launch_epoch,
                                  cost_of=self.cost_of,
                                  estimator=lambda *args, **kwargs: guess,
                                  stop=before_epoch, scenarios=self.scenarios_dir)
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
        if stopped := self.stopped or self.halted:
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
    [--mock] [--fake] [--model M] [--analysis-only <milestone 1 audit dir>]
    [--sandbox | --no-sandbox] [--steer]``.

    ``--question`` with ``--budget`` is the shortest brief: the question and the budget,
    everything else left to L2 (spec §1.1); the audit keeps it as its ``brief.md``.

    L2's code runs in the sandbox (``experiments.l2.sandbox``) in every audit that needs
    ``--live``, and with ``--sandbox`` (or ``L2_SANDBOX=1``) in a scripted one; the audit
    first checks the sandbox and does not start when a property fails. ``--no-sandbox``
    runs that code as the user instead.

    Without ``--fake``, L2 is an LLM (``--model``, default ``agent.MODEL``) and its own
    calls are live (OpenRouter) even when the experiments run on mocks (``--mock``).

    ``--steer`` (or ``COSMOS_STEER=1``, or ``COSMOS_STEER_DIR``) lets the audit be steered
    through its mailbox (``experiments.l2.steer``). When L2 is a model, a steered audit
    starts only if it will report a sandbox (``Audit.sandbox_report``): code L2 wrote
    could otherwise write the mailbox. With ``COSMOS_REQUIRE_SANDBOX=1`` that holds for
    every audit, steered or not. Either turns the sandbox on and refuses
    ``--no-sandbox``."""
    import argparse
    import os

    from experiments.l2 import agent
    from experiments.l2 import steer as steer_
    from experiments.l2.audit_report import write_report
    from experiments.l2.fake import ANALYSIS, FULL, PROMPT, fake_model
    from experiments.l2.progress import reporter
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
    parser.add_argument("--sandbox", action="store_true",
                        help="run L2's code (run_python, the check of a written scenario) in "
                             "a sandbox: no network, reads only the runs, writes only "
                             "figures/ (also L2_SANDBOX=1); refused when it cannot start. "
                             "On by default for an audit that needs --live")
    parser.add_argument("--no-sandbox", action="store_true",
                        help="run L2's code as you, with the network and every file you can "
                             "read, also in an audit that needs --live (and whatever "
                             "L2_SANDBOX says); refused for a steered audit whose L2 is a "
                             "model, and with COSMOS_REQUIRE_SANDBOX=1")
    parser.add_argument("--quiet", action="store_true",
                        help="show no progress (the live status panel, or lines when the "
                             "output is not a terminal) while the audit runs")
    parser.add_argument("--steer", action="store_true",
                        help="read requests from the audit's mailbox (COSMOS_STEER_DIR, else "
                             "<audit>/l2/.cosmos-steer): pause, resume, stop, budget, "
                             "answers and notes, sent with python -m experiments.l2.steer "
                             "or by Cosmos")
    args = parser.parse_args(argv)
    if (args.question is None) != (args.budget is None):
        parser.error("--question and --budget go together (a --brief sets its own budget)")
    if args.time_limit is not None and not args.time_limit > 0:
        parser.error("--time-limit must be a positive number of seconds")
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
        from dotenv import load_dotenv

        load_dotenv(m1.REPO / ".env")  # OPENROUTER_API_KEY
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise SystemExit(f"{what} need OPENROUTER_API_KEY (environment or .env)")
    out = args.out or Path(f"logs/l2-audit/{time.strftime('%Y%m%d-%H%M%S')}"
                           + ("-analysis" if args.analysis_only else "")
                           + ("-mock" if args.mock else "") + ("-fake" if args.fake else ""))
    try:  # refused before anything is made
        steering = steer_.configure(out, flag=args.steer)
    except steer_.SteerError as error:
        raise SystemExit(str(error)) from None
    # Why this audit must report a sandbox, if it must: asked for outright, or steered
    # while L2 is a model, whose code could otherwise write the mailbox.
    required = ("COSMOS_REQUIRE_SANDBOX=1" if steer_.sandbox_required() else
                steer_.NEEDS_SANDBOX if steering is not None and live_l2 else None)
    asked = args.sandbox or os.environ.get("L2_SANDBOX") == "1"
    if args.no_sandbox and (required or args.sandbox):
        raise SystemExit("--no-sandbox, but " + (required or "--sandbox was given too")
                         + ": not started")
    sandbox = None
    # On when asked for or required, and by default whenever L2's code is a model's
    # (every audit that needs --live): --no-sandbox is the only way to run that code as
    # the user, with the network and every file the user can read.
    if not args.no_sandbox and (asked or required or live_l2 or live_runs):
        from experiments.l2 import sandbox as sandbox_

        found = sandbox_.check()  # before anything is created or spent
        if not found.passed:
            raise SystemExit("\n".join([
                "the sandbox does not hold here, so the audit does not start:",
                *found.failed(),
                *([] if asked or required else [
                    "A live audit runs L2's code in the sandbox by default. Install it "
                    "(docs/level2-howto.md: bubblewrap on Linux), or pass --no-sandbox to "
                    "run that code as you, with the network and every file you can read."])]))
        sandbox = sandbox_.detect()
    with tempfile.TemporaryDirectory() as tmp:
        brief_path = args.brief
        if args.question is not None:
            brief_path = Path(tmp) / "brief.md"
            brief_path.write_text(f"---\nbudget_usd: {args.budget}\n---\n"
                                  f"{args.question.strip()}\n")
        planned = Audit(out, parse_brief(brief_path), mock=args.mock,
                        analysis=args.analysis_only, sandbox=sandbox)
        if required and planned.sandbox_report() is None:
            raise SystemExit(
                required + ", but " + (
                    "this audit lets L2 write scenarios, whose code runs outside the "
                    "sandbox (in this process, and in each epoch with the provider's key "
                    "and the network): not started. Leave scenario out of the brief's "
                    "allowed_changes" if planned.writes_scenarios else
                    f"{planned.scenarios_dir} holds written scenarios, which this audit "
                    "imports into its own process, outside the sandbox: not started"))
        if steering is not None and sandbox is not None:
            visible = [out / FIGURES, out / "experiments",
                       *([args.analysis_only] if args.analysis_only else []),
                       *(m1.REPO / part for part in written_.CHECK_READS),
                       *map(Path, sandbox.always_reads())]
            if inside := steer_.under(steering[0].path, visible):
                raise SystemExit(f"the mailbox {steering[0].path} is inside {inside}, which "
                                 "the sandbox can read: put it outside (COSMOS_STEER_DIR)")
        audit = Audit.create(brief_path, out, mock=args.mock, analysis=args.analysis_only,
                             history=args.history, sandbox=sandbox)
    if steering is not None:
        mailbox, limit = steering
        audit.steering = steer_.Steering(mailbox, audit, max_budget=limit,
                                         sandbox=audit.sandbox_report())
    tools = Tools(audit, on_end=write_report)
    model = None if args.fake else args.model or agent.MODEL
    if model is not None:
        print(f"L2 is {model}; budget ${audit.brief.budget_usd:.2f} (L2's calls "
              f"included); audit: {out}. Ctrl-C once: the running experiment finishes, "
              "then L2 concludes; twice: stop at once.", flush=True)
        print(f"L2's code runs in the sandbox ({sandbox.kind})." if sandbox is not None else
              "L2's code runs as you, outside a sandbox (" + (
                  "--no-sandbox" if args.no_sandbox else
                  "a scripted audit; --sandbox turns one on") + ").", flush=True)
    try:
        with reporter(audit, quiet=args.quiet, model=model or "the fake L2"):
            if model is None:
                script = ANALYSIS if args.analysis_only else FULL
                agent.run_l2(tools, fake_model(script), PROMPT, time_limit=args.time_limit)
            else:
                agent.run_l2(tools, model, agent.SYSTEM_PROMPT, time_limit=args.time_limit)
    except steer_.SteerError as error:  # the mailbox could not be opened: L2 never ran
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:  # a second Ctrl-C: the record is written all the same
        report = out / "report.md"
        print("stopped by hand; " + (f"report: {report}" if report.exists() else
                                     "no report was written"))
        raise SystemExit(130) from None
    budget = audit.budget()
    print(f"spent ${budget['spent_usd']:.4f} of ${budget['budget_usd']:.2f}")
    print(f"report: {out / 'report.md'}")
    if (out / "writeup.md").exists():
        print(f"write-up: {out / 'writeup.md'}")
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
