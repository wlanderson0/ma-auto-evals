"""Run a brief's fixed variants, epoch by epoch, and record the audit (spec §5, §8).

Usage (live; reads OPENROUTER_API_KEY from the environment or a gitignored .env)::

    uv run python -m experiments.l2.experiment --brief experiments/l2/briefs/milestone1.md \
        --epochs 5 [--confirm]

Offline, on the scripted mock models (no network, no cost)::

    uv run python -m experiments.l2.experiment --brief ... --epochs 2 --mock

Variants may be built on scenarios L2 wrote (``experiments.l2.written``):
``--scenarios <dir>`` names the directory they are in.

``run_experiment`` builds every variant (``apply``), checks it (``check``), records
it (rejected ones too), estimates the cost against the brief's budget, then runs
the accepted variants one epoch at a time: each epoch is one subprocess
(``--epoch <e>``) making one ``inspect_eval`` call over all accepted variant tasks,
so variants within an epoch run in parallel and every variant gets epoch ``e``
before any gets ``e + 1``. One process per epoch avoids Inspect's per-loop model
cache, as ``experiments.run`` does. The budget is checked between epochs, so
spending can exceed it by up to one epoch; stopping a sample mid-run comes with
the audit-wide ledger (spec §7).

The audit directory ``out/`` holds ``brief.md`` (a copy), ``variants.jsonl``
(every variant with its problems), ``decisions.json`` (the launch decision and
any stop), ``runs.jsonl`` (one ``VariantRun`` per variant, with its log paths in
epoch order), ``cost.json`` and the eval logs under ``logs/epoch-<e>/``. Results
are read from the logs (``experiments.l2.results``), never from these files.

The agents', judge's and probes' models carry ``RUN_CONFIG`` (``run_models``,
``run_roles``), with a larger reply cap for reasoning models (``run_config``), recorded in
``decisions.json``; an inner L0/L1 auditor keeps its own settings.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from inspect_ai import eval as inspect_eval
from inspect_ai import task_with
from inspect_ai.log import EvalLog, read_eval_log
from inspect_ai.model import GenerateConfig, Model, get_model

from experiments.l2 import written as written_
from experiments.l2.apply import ApplyError, apply, recorded
from experiments.l2.brief import parse_brief
from experiments.l2.cost import PRICES, PRICES_CHECKED, log_cost, price
from experiments.l2.inner import with_agent
from experiments.l2.model import InnerSpec, L2Brief, Mutation, Problems, Variant, VariantRun
from experiments.l2.validity import check
from experiments.run import (
    AUDITOR,
    JUDGE,
    LIMITS,
    MAX_TOKENS,
    TARGETS,
    _mock_models,
    build_task,
    l2_metadata,
)
from experiments.scenarios import SCENARIOS, Scenario, build_team
from orbit_audit.branches import AuditLimits
from orbit_audit.scope import AuditScope

Launch = Callable[[Path, list[str], int, Path, bool, Path | None], dict[str, str]]
"""``(brief, variant ids, epoch, out, mock, scenarios) -> {variant id: eval log path}``;
``scenarios`` is the directory of written scenarios, if any."""
CostOf = Callable[[EvalLog], dict[str, Any]]
REPO = Path(__file__).resolve().parents[2]
"""Where ``python -m experiments...`` must run from (the package is not installed)."""
ATTEMPT_TIMEOUT, MAX_RETRIES, SAMPLE_RETRIES = 45, 4, 1
"""A provider call that has not answered in ``ATTEMPT_TIMEOUT`` seconds is abandoned and
retried, up to ``MAX_RETRIES`` times (Inspect sets no timeout by default, and in the
first live run three samples hung on a call that never returned); a sample that errors
anyway (e.g. a provider's 404) is run again ``SAMPLE_RETRIES`` time(s), recorded in the
log's ``error_retries``."""


# ------------------------------------------------------------------ building
def base_scenario(name: str, mock: bool,
                  written: written_.WrittenScenarios = written_.NONE) -> Scenario:
    """The base scenario a variant is built on: a seed scenario on the mock models or on
    the live models of ``experiments.run.TARGETS``; a written one with ``{}`` (its code
    sets its own models)."""
    models = {} if mock or name not in SCENARIOS else TARGETS[name]
    return written_.scenario(name, written)(models)


def build(variant: Variant, mock: bool, written: written_.WrittenScenarios = written_.NONE
          ) -> tuple[Scenario | None, Scenario | None, Problems]:
    """``(base, built, problems)``; a variant that cannot be built is a rejection. On the
    mock models every agent runs on the task's mock, whatever model a mutation gave it
    (it is still checked against the price table). A variant of a written scenario
    records the hash of its code (``scenario_sha256``, L2-17)."""
    if not written_.known(variant.base, written):
        return None, None, Problems(rejections=(f"unknown base scenario {variant.base!r}",))
    base = base_scenario(variant.base, mock, written)
    try:
        built = apply(base, variant)
    except ApplyError as error:
        return base, None, check(base, None, variant,
                                 build_error=f"{type(error).__name__}: {error}")
    if mock:
        team = dataclasses.replace(built.team, models={})
        built = dataclasses.replace(built, team=team, task=build_team(built.name, team))
    if variant.base not in SCENARIOS:  # so a written one
        built = dataclasses.replace(built, variant={
            **built.variant, "scenario_sha256": written[variant.base].sha256})
    return base, built, check(base, built, variant)


RUN_CONFIG = GenerateConfig(max_tokens=MAX_TOKENS, reasoning_effort="high")
"""The settings attached to every model of a run: the agents', the judge's and the
probes'. Models without reasoning are expected to ignore the effort."""
REASONING_MAX_TOKENS = 4000
"""The reply cap of a model in ``REASONING_MODELS``, in place of ``MAX_TOKENS``: its
reasoning counts against the cap, and at high effort gpt-5-nano spent all 1500 tokens
reasoning and replied with nothing."""
REASONING_MODELS = frozenset({
    "openrouter/openai/gpt-5-nano", "openrouter/openai/gpt-5.4",
    "openrouter/deepseek/deepseek-r1-0528", "openrouter/google/gemini-2.5-pro",
})
"""Models that reason before they reply (``cost.PRICES``' reasoning models)."""


def run_config(model: str | Model) -> GenerateConfig:
    """``RUN_CONFIG``, with ``REASONING_MAX_TOKENS`` for a reasoning model."""
    name = model if isinstance(model, str) else str(model)
    return (RUN_CONFIG.merge(GenerateConfig(max_tokens=REASONING_MAX_TOKENS))
            if name in REASONING_MODELS else RUN_CONFIG)


def configured(model: str | Model) -> Model:
    """``model`` with its ``run_config`` attached (uncached: Inspect's cached clients
    belong to the event loop of the eval that made them)."""
    if isinstance(model, str):
        return get_model(model, config=run_config(model), memoize=False)
    attached = copy.copy(model)
    attached.config = model.config.merge(run_config(model))
    return attached


def run_models(built: Scenario, models: dict[str, Any]) -> tuple[Scenario, Model]:
    """``(built, default)``: the variant to run and its task's default model, with
    ``RUN_CONFIG`` attached (the judge's and probes' roles: ``run_roles``).

    Every agent's spec carries ``RUN_CONFIG`` (``run_config``: a reasoning model's reply
    cap is ``REASONING_MAX_TOKENS``) as its ``generate_config``, which Orbit
    merges over the agent's model, so agents on several models run under the same
    settings as agents on one. Settings the scenario gave an agent itself
    (``team_setup(generate_configs=...)``, e.g. a lower reasoning effort) win over
    ``RUN_CONFIG``. When every agent runs on one model, their names are also
    cleared and that model is the default (probes then ask it too, as the agent's own
    model)."""
    one = one_model(built, models["target"])
    setup = built.task["setup"]
    setup = setup.model_copy(update={"agents": [
        spec.model_copy(update={
            "generate_config": run_config(spec.model or models["target"]).merge(
                spec.generate_config or GenerateConfig()),
            **({"model": None} if one is not None else {})})
        for spec in setup.agents]})
    built = dataclasses.replace(built, task={**built.task, "setup": setup})
    return built, configured(models["target"] if one is None else one)


def run_roles(models: dict[str, Any]) -> dict[str, Model]:
    """The judge's and (on the mocks) the probes' roles, with ``RUN_CONFIG`` attached."""
    return {role: configured(models[role]) for role in ("judge", "probe") if role in models}


def one_model(built: Scenario, default: str | Model) -> str | Model | None:
    """The one model all of ``built``'s agents run on (``default`` for an agent with
    none of its own), else ``None``."""
    used = {spec.model or default for spec in built.task["setup"].agents}
    return next(iter(used)) if len(used) == 1 else None


def models_for(mock: bool) -> dict[str, Any]:
    if mock:
        models = _mock_models()
        models["probe"] = models["judge"]
        return models
    return {"target": AUDITOR, "auditor": AUDITOR, "judge": JUDGE}


# ----------------------------------------------------------- one epoch, run
def run_epoch(brief_path: Path, ids: Sequence[str], epoch: int, out: Path,
              mock: bool, scenarios: Path | None = None) -> dict[str, str]:
    """Run epoch ``epoch`` of the variants ``ids`` as one ``inspect_eval`` call; return
    each variant's log path, also written to ``out/epoch-<e>.json``. ``scenarios``: the
    directory of written scenarios the variants may be built on."""
    written = written_.load(scenarios)
    brief = parse_brief(brief_path, written)
    by_id = {v.id: v for v in brief.variants or ()}
    models = models_for(mock)
    tasks, names = [], {}
    for vid in ids:
        _, built, problems = build(by_id[vid], mock, written)
        if built is None or problems.rejected:  # the parent never sends these
            raise ValueError(f"variant {vid} is rejected: {problems.rejections}")
        condition = by_id[vid].inner.condition
        metadata = l2_metadata(built, condition, epoch=epoch)
        built, default = run_models(built, models)
        tasks.append(task_with(build_task(built, condition, models,
                                          limits=by_id[vid].inner.limits or LIMITS,
                                          metadata=metadata), model=default))
        names[built.name] = vid
    # No outer id_scope(sequential_ids()) as run_condition has: the runner gives every
    # sample its own id scope, and a counter shared by tasks running in parallel would
    # not be reproducible anyway.
    logs = inspect_eval(
        tasks, model=configured(models["target"]),
        log_dir=str(out / "logs" / f"epoch-{epoch}"),
        display="none", max_tasks=len(tasks), epochs=1, fail_on_error=False,
        score_on_error=True, model_roles=run_roles(models),
        attempt_timeout=ATTEMPT_TIMEOUT, max_retries=MAX_RETRIES,
        retry_on_error=SAMPLE_RETRIES)
    paths = {names[log.eval.task]: relative(log.location, out) for log in logs}
    (out / f"epoch-{epoch}.json").write_text(json.dumps(paths, indent=1, sort_keys=True))
    return paths


RUNNING: set[subprocess.Popen] = set()
"""Epoch subprocesses running now (``stop_running`` stops them)."""


def stop_running() -> None:
    """Stop every running epoch subprocess, with anything it started (e.g. on a second
    Ctrl-C, which the epochs do not receive themselves)."""
    for process in list(RUNNING):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)


def launch_subprocess(brief_path: Path, ids: list[str], epoch: int, out: Path,
                      mock: bool, scenarios: Path | None = None) -> dict[str, str]:
    """``run_epoch`` in a fresh process (Inspect caches model clients per event loop), in
    a session of its own, so a Ctrl-C in the terminal reaches only the audit, which
    decides whether the epoch finishes (``stop_running``)."""
    command = [sys.executable, "-m", "experiments.l2.experiment", "--brief",
               str(Path(brief_path).resolve()), "--variants", ",".join(ids), "--epoch",
               str(epoch), "--out", str(Path(out).resolve())]
    if mock:
        command.append("--mock")
    if scenarios is not None:
        command += ["--scenarios", str(Path(scenarios).resolve())]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          cwd=REPO, start_new_session=True) as process:
        RUNNING.add(process)
        try:
            _, stderr = process.communicate()
        except BaseException:  # e.g. Ctrl-C in the fixed-grid CLI: never leave it running
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            raise
        finally:
            RUNNING.discard(process)
    record = out / f"epoch-{epoch}.json"
    if process.returncode != 0 or not record.exists():
        how = (f"stopped by signal {-process.returncode}" if process.returncode < 0
               else f"exit {process.returncode}")
        raise RuntimeError(f"epoch {epoch} failed ({how}):\n{stderr[-3000:]}")
    return json.loads(record.read_text())


# ------------------------------------------------------------- estimating
SYSTEM_TOKENS, POST_TOKENS, OUTPUT_TOKENS = 300, 120, 100
"""Rough sizes for the token-count estimate: a system prompt plus task, a delivered
post, and one generation (posts are under 80 words)."""
FREE_PROBES = 3


def scheduled_steps(built: Scenario) -> list[tuple[str, ...]]:
    """The steps the run will take, mid-run activations included."""
    steps = [tuple(step.agents) for step in built.task["execution"].plan.steps]
    for activation in built.activations:
        done = activation.after_turn
        steps = steps[:done] + list(with_agent(tuple(steps[done:]), activation))
    return steps


def token_estimate(built: Scenario, judge: str = JUDGE, default: str = AUDITOR) -> float:
    """A rough per-sample cost from token counts (spec §5): each turn re-reads every
    earlier post, so cost grows faster than the number of rounds. Agents without a
    model of their own run on ``default`` (the task's model)."""
    steps = scheduled_steps(built)
    models = {spec.name: spec.model or default for spec in built.task["setup"].agents}
    usd = 0.0
    for turn, step in enumerate(steps):
        for agent in step:
            per_input, per_output = price(models[agent])
            usd += ((SYSTEM_TOKENS + turn * POST_TOKENS) * per_input
                    + OUTPUT_TOKENS * per_output) / 1e6
    final_context = SYSTEM_TOKENS + len(steps) * POST_TOKENS
    judge_in, judge_out = price(judge)
    for target in built.targets:
        posts = sum(target in step for step in steps)
        usd += posts * (200 * judge_in + 2 * judge_out) / 1e6  # one judge call per post
        per_input, per_output = price(models[target])
        probes = (1 if built.probes else 0) + (FREE_PROBES if built.free_probes else 0)
        usd += probes * (final_context * per_input + 20 * per_output) / 1e6
    return usd


def variant_from(record: dict[str, Any]) -> Variant:
    """A variant back from its recorded form (``apply.recorded``)."""
    inner = record["inner"]
    scope = inner["scope"]
    return Variant(
        id=record["id"], base=record["base"], rationale=record["rationale"],
        mutations=tuple(Mutation(**m) for m in record["mutations"]),
        inner=InnerSpec(condition=inner["condition"], brief=inner["brief"],
                        opening=tuple(tuple(m) for m in inner["opening"]),
                        scope=None if scope is None else AuditScope(**scope),
                        limits=None if inner["limits"] is None else AuditLimits(**inner["limits"])))


@dataclasses.dataclass
class History:
    """Per-sample costs of earlier runs, from audit directories."""

    runs: list[tuple[Variant, list[float]]]
    skipped: list[str]

    @classmethod
    def read(cls, audits: Sequence[Path], cost_of: CostOf) -> History:
        runs, skipped = [], []
        for audit in audits:
            for row in _read_jsonl(Path(audit) / "runs.jsonl"):
                costs = []
                for path in row["logs"]:
                    resolved = resolve(path, Path(audit))
                    if not resolved.exists():
                        skipped.append(f"missing log {resolved}")
                        continue
                    costs.extend(s["usd"] for s in cost_of(read_eval_log(str(resolved)))
                                 .get("samples", []))
                if costs:
                    runs.append((variant_from(row["variant"]), costs))
        return cls(runs=runs, skipped=skipped)

    def per_sample(self, variant: Variant, built: Scenario,
                   written: written_.WrittenScenarios = written_.NONE) -> tuple[float, str] | None:
        """The mean per-sample cost of earlier runs of the same variant, else of the same
        base and condition scaled by the token estimates' ratio (a longer variant costs
        more than its control), with how it was got; ``None`` without history."""
        same = [c for v, costs in self.runs for c in costs
                if (v.base, v.mutations, v.inner) == (variant.base, variant.mutations,
                                                      variant.inner)]
        if same:
            return sum(same) / len(same), "earlier runs"
        related = [(v, costs) for v, costs in self.runs
                   if (v.base, v.inner.condition) == (variant.base, variant.inner.condition)]
        if not related:
            return None
        scaled = []
        for v, costs in related:
            _, reference, _ = build(v, mock=False, written=written)
            if reference is None:
                continue
            ratio = token_estimate(built) / token_estimate(reference)
            scaled.extend(c * ratio for c in costs)
        if not scaled:
            return None
        return sum(scaled) / len(scaled), "earlier runs, scaled by token counts"


def estimate(accepted: list[tuple[Variant, Scenario]], epochs: int,
             history: Sequence[Path], cost_of: CostOf, mock: bool = False,
             written: written_.WrittenScenarios = written_.NONE) -> dict[str, Any]:
    """The experiment's estimated cost, per variant, with its basis. Mock runs are free
    and ignore ``history`` (whose logs may be live). ``written``: the scenarios earlier
    runs may be built on."""
    if mock:
        return {"usd": 0.0, "basis": ["mock models (free)"],
                "per_variant": {v.id: 0.0 for v, _ in accepted}, "epochs": epochs}
    known = History.read(history, cost_of)
    per_variant, bases = {}, set()
    for variant, built in accepted:
        found = known.per_sample(variant, built, written)
        per_sample, basis = found if found is not None else (token_estimate(built),
                                                             "token counts")
        bases.add(basis)
        per_variant[variant.id] = per_sample * epochs
    result = {"usd": sum(per_variant.values()), "basis": sorted(bases),
              "per_variant": per_variant, "epochs": epochs}
    if known.skipped:
        result["history_skipped"] = known.skipped
    return result


# ------------------------------------------------------------------ record
def relative(path: str | Path, out: Path) -> str:
    """``path`` relative to the audit directory when it is inside it, so the directory
    can be moved or shared."""
    path, out = Path(path).resolve(), Path(out).resolve()
    return str(path.relative_to(out)) if path.is_relative_to(out) else str(path)


def resolve(path: str, out: Path) -> Path:
    return Path(path) if Path(path).is_absolute() else Path(out) / path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def _run_row(run: VariantRun, errors: list[str], built: Scenario | None) -> dict[str, Any]:
    """A ``runs.jsonl`` row; a variant of a written scenario also gets the hash of its
    code (``scenario_sha256``)."""
    row = {"variant": recorded(run.variant), "rejections": list(run.problems.rejections),
           "warnings": list(run.problems.warnings), "cost_usd": run.cost_usd,
           "logs": list(run.logs), "epochs_done": run.epochs_done, "errors": errors}
    if built is not None and "scenario_sha256" in built.variant:
        row["scenario_sha256"] = built.variant["scenario_sha256"]
    return row


def provenance(mock: bool, checked: list) -> dict[str, Any]:
    """What a later reader needs to rebuild the runs: models, prices and the code."""
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", *args], capture_output=True, text=True, cwd=REPO,
                                  timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    models = {v.id: dict(b.team.models) for v, b, _ in checked if b is not None and b.team}
    return {"commit": git("rev-parse", "HEAD"),
            "dirty": None if (status := git("status", "--porcelain")) is None else bool(status),
            "models": ({"all": "mockllm (scripted mocks)"} if mock else
                       {"default_target": AUDITOR, "judge": JUDGE, "agents": models}),
            "prices": PRICES, "prices_checked": PRICES_CHECKED}


def run_experiment(brief_path: Path, epochs: int, out: Path, *, mock: bool = False,
                   confirm: bool = False, history: Sequence[Path] = (),
                   launch: Launch = launch_subprocess, cost_of: CostOf = log_cost,
                   estimator: Callable[..., dict[str, Any]] = estimate,
                   stop: Callable[[], str | None] | None = None,
                   scenarios: Path | None = None) -> list[VariantRun]:
    """Build, check, record and run the brief's variants for ``epochs`` epochs.

    ``out`` must be new or empty. Every epoch runs from the recorded copy of the brief
    (``out/brief.md``), so editing the brief during a run changes nothing. A failed
    epoch stops the run: the record keeps every finished epoch, and lists the failed
    epoch's logs and cost as far as they can be read, before the error is re-raised.
    ``scenarios``: the directory of written scenarios the variants may be built on."""
    brief_path, out = Path(brief_path), Path(out)
    written = written_.load(scenarios)
    brief: L2Brief = parse_brief(brief_path, written)
    if not brief.variants:
        raise ValueError(f"{brief_path} fixes no variants: run_experiment runs fixed grids")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty: each audit gets a directory of its own")
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(brief_path, out / "brief.md")
    recorded_brief = out / "brief.md"

    checked = []
    for variant in brief.variants:
        _, built, problems = build(variant, mock, written)
        checked.append((variant, built, problems))
    _jsonl(out / "variants.jsonl", [
        {**recorded(v), "rejections": list(p.rejections), "warnings": list(p.warnings),
         "status": "rejected" if p.rejected else ("warned" if p.warnings else "accepted")}
        for v, _, p in checked])
    accepted = [(v, b) for v, b, p in checked if not p.rejected and b is not None]

    guess = estimator(accepted, epochs, history, cost_of, mock, written)
    over = guess["usd"] > brief.budget_usd
    decisions: dict[str, Any] = {
        "mode": brief.mode, "epochs": epochs, "mock": mock, "budget_usd": brief.budget_usd,
        "run_config": RUN_CONFIG.model_dump(exclude_none=True),
        "reasoning_models": {"max_tokens": REASONING_MAX_TOKENS,
                             "models": sorted(REASONING_MODELS)},
        "estimate": guess, "over_budget": over, "confirmed": confirm,
        "launched": bool(accepted) and (not over or confirm),
        "accepted": [v.id for v, _ in accepted],
        "rejected": [v.id for v, _, p in checked if p.rejected],
        "stopped": None, "failed_epoch": None,
        "provenance": provenance(mock, checked),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if over and not confirm:
        decisions["why_not_launched"] = (
            f"estimated ${guess['usd']:.2f} ({', '.join(guess['basis'])}) exceeds the "
            f"budget ${brief.budget_usd:.2f}; rerun with confirm=True to launch")

    state = _State(checked)
    # On disk before anything is spent: a run killed mid-epoch still leaves the launch
    # decision, the estimate and every variant's (empty) run record.
    state.write(out, decisions, brief)
    ids = [v.id for v, _ in accepted]
    for epoch in range(1, epochs + 1) if decisions["launched"] else ():
        total = sum(state.spent.values())
        if total >= brief.budget_usd:
            decisions["stopped"] = (f"before epoch {epoch}: spent ${total:.4f} of the "
                                    f"${brief.budget_usd:.2f} budget")
            break
        if stop is not None and (why := stop()):  # e.g. the caller's time is up
            decisions["stopped"] = f"before epoch {epoch}: {why}"
            break
        try:
            paths = launch(recorded_brief, ids, epoch, out, mock, scenarios)
            if set(paths) != set(ids):
                raise RuntimeError(f"epoch {epoch} returned logs for {sorted(paths)}, "
                                   f"expected {sorted(ids)}")
            logs = {vid: read_eval_log(str(resolve(paths[vid], out))) for vid in ids}
            costs = {vid: cost_of(logs[vid])["usd"] for vid in ids}
        except Exception as error:
            decisions["stopped"] = f"epoch {epoch} failed: {type(error).__name__}: {error}"
            decisions["failed_epoch"] = state.salvage(out, epoch, cost_of)
            decisions["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            state.write(out, decisions, brief)
            raise
        state.add(epoch, paths, logs, costs)
        failed = [vid for vid in ids if logs[vid].samples
                  and all(s.error is not None for s in logs[vid].samples)]
        if failed == ids:
            decisions["stopped"] = (f"after epoch {epoch}: every sample errored (e.g. "
                                    f"{logs[ids[0]].samples[0].error.message[:200]})")
            break
        state.write(out, decisions, brief)
    decisions["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return state.write(out, decisions, brief)


SAFEGUARD_MARKS = ("content_filter", "safeguards flagged")
"""Text in an error that a model provider's safeguards caused it (e.g. Anthropic's "Cyber
safeguards flagged this message", sent back as the reply)."""


def safeguard_blocks(sample: Any) -> str | None:
    """How a model provider's safeguards blocked this sample, in words, or ``None``: the
    model calls whose reply was withheld (``stop_reason`` ``content_filter``), by model,
    and whether the sample's error comes from them. A blocked reply is not a refusal by
    the model: the provider withheld it, so the agent's behaviour there is unknown."""
    counts: dict[str, int] = {}
    for event in sample.events or []:
        if getattr(event, "event", None) != "model" or event.output is None:
            continue
        if any(choice.stop_reason == "content_filter" for choice in event.output.choices):
            counts[event.model] = counts.get(event.model, 0) + 1
    error = sample.error.message if sample.error is not None else ""
    from_error = any(mark in error.lower() for mark in SAFEGUARD_MARKS)
    if not counts and not from_error:
        return None
    by_model = ", ".join(f"{n} from {model}" for model, n in sorted(counts.items()))
    return ("blocked by the model provider's safeguards: "
            + (f"replies withheld ({by_model})" if counts else "")
            + ("; " if counts and from_error else "")
            + ("the sample's error comes from a blocked reply" if from_error else "")
            + "; the agent's behaviour there is unknown, not a refusal")


class _State:
    """Logs, spend and errors per variant, as the epochs come in."""

    def __init__(self, checked: list) -> None:
        self.checked = checked
        self.logs: dict[str, list[str]] = {v.id: [] for v, _, _ in checked}
        self.spent: dict[str, float] = {v.id: 0.0 for v, _, _ in checked}
        self.errors: dict[str, list[str]] = {v.id: [] for v, _, _ in checked}
        self.by_epoch: list[dict[str, Any]] = []

    def add(self, epoch: int, paths: dict[str, str], logs: dict[str, EvalLog],
            costs: dict[str, float]) -> None:
        for vid, path in paths.items():
            self.logs[vid].append(path)
            self.spent[vid] += costs[vid]
            for sample in logs[vid].samples or []:
                blocked = safeguard_blocks(sample)
                if blocked:
                    self.errors[vid].append(f"epoch {epoch}: {blocked}")
                if sample.error is not None:
                    self.errors[vid].append(f"epoch {epoch}: {sample.error.message}")
            if logs[vid].status != "success" and not logs[vid].samples:
                self.errors[vid].append(f"epoch {epoch}: log status {logs[vid].status}")
        self.by_epoch.append({"epoch": epoch, "usd": sum(costs.values())})

    def salvage(self, out: Path, epoch: int, cost_of: CostOf) -> dict[str, Any]:
        """The logs a failed epoch left on disk, and what they cost (as far as known)."""
        found: dict[str, Any] = {}
        for path in sorted((out / "logs" / f"epoch-{epoch}").rglob("*.eval")):
            try:
                found[relative(path, out)] = cost_of(read_eval_log(str(path)))["usd"]
            except Exception as error:  # the cost is unknown, the log is still listed
                found[relative(path, out)] = f"cost unknown: {type(error).__name__}: {error}"
        known = [usd for usd in found.values() if isinstance(usd, float)]
        self.by_epoch.append({"epoch": epoch, "usd": sum(known), "failed": True})
        return {"epoch": epoch, "logs": found}

    def write(self, out: Path, decisions: dict[str, Any], brief: L2Brief) -> list[VariantRun]:
        runs = [VariantRun(variant=v, problems=p, cost_usd=self.spent[v.id],
                           logs=tuple(self.logs[v.id]), epochs_done=len(self.logs[v.id]))
                for v, _, p in self.checked]
        built = {v.id: b for v, b, _ in self.checked}
        _jsonl(out / "runs.jsonl", [_run_row(run, self.errors[run.variant.id],
                                             built[run.variant.id]) for run in runs])
        (out / "decisions.json").write_text(json.dumps(decisions, indent=1, sort_keys=True))
        (out / "cost.json").write_text(json.dumps({
            "budget_usd": brief.budget_usd, "estimate": decisions["estimate"],
            "spent_usd": sum(e["usd"] for e in self.by_epoch),
            "by_variant": self.spent, "by_epoch": self.by_epoch,
            "prices_checked": PRICES_CHECKED,
            "note": "the budget is checked between epochs, so spending can exceed it by up "
                    "to one epoch; a failed epoch's spend is in by_epoch, not by_variant"},
            indent=1, sort_keys=True))
        return runs


# --------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--brief", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--mock", action="store_true", help="offline scripted models")
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--confirm", action="store_true",
                        help="launch even if the estimate exceeds the budget")
    parser.add_argument("--history", type=Path, nargs="*", default=(),
                        help="earlier audit directories to estimate cost from")
    parser.add_argument("--scenarios", type=Path, default=None,
                        help="the directory of written scenarios variants may be built on")
    parser.add_argument("--variants", help=argparse.SUPPRESS)  # epoch mode (subprocess)
    parser.add_argument("--epoch", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.mock:
        from dotenv import load_dotenv

        load_dotenv(".env")  # OPENROUTER_API_KEY from the gitignored .env
    if args.epoch is not None:
        run_epoch(args.brief, args.variants.split(","), args.epoch, args.out, args.mock,
                  args.scenarios)
        return
    out = args.out or Path(f"logs/l2/{time.strftime('%Y%m%d-%H%M%S')}"
                           + ("-mock" if args.mock else ""))
    runs = run_experiment(args.brief, args.epochs, out, mock=args.mock, confirm=args.confirm,
                          history=args.history, scenarios=args.scenarios)
    decisions = json.loads((out / "decisions.json").read_text())
    for run in runs:
        state = "rejected" if run.problems.rejected else f"{run.epochs_done} epoch(s)"
        print(f"{run.variant.id:4} {state:12} ${run.cost_usd:.4f}"
              + (f"  warnings: {'; '.join(run.problems.warnings)}" if run.problems.warnings
                 else "")
              + (f"  rejected: {'; '.join(run.problems.rejections)}" if run.problems.rejected
                 else ""))
    if not decisions["launched"]:
        print("not launched:", decisions.get("why_not_launched", "no accepted variants"))
    if decisions["stopped"]:
        print("stopped:", decisions["stopped"])
    print(f"audit: {out}")


if __name__ == "__main__":
    main()
