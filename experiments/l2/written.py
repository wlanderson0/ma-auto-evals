"""Scenarios written by L2 (spec ``docs/level2-interface.md`` §3.5; plan
``docs/level2-milestone4.md``).

A written scenario is a Python module defining one function, ``make(models: dict) ->
Scenario``, built with the helpers the seed scenarios use (``team_setup`` /
``build_team``, ``Probe``, an outcome function). An audit keeps them in
``<audit>/scenarios/<name>.py``, each with ``<name>.sha256``, the hash of its code.

``load(directory)`` imports every module there; ``scenario(name, written)`` is the one
lookup the L2 path uses for a scenario factory: the seed scenarios
(``experiments.scenarios.SCENARIOS``) first, then the written ones. A written factory is
called with ``{}``: its code sets its agents' models itself.

``dry_run`` runs one ``baseline`` sample of a written scenario on the generic mocks
(``generic_models``), which check plumbing only: that the scenario runs end to end and
its scorers run on the log, not how agents behave. The scripted mocks of
``experiments.run`` are tuned to the seed scenarios and stay as they are.

``python -m experiments.l2.written --check <file> --name <name>`` is the check
``write_scenario`` runs on new code, in a subprocess without provider keys (``check``):
the code builds a ``Scenario`` named ``name`` whose task is ``build_team(name, team)``
(variants rebuild the task from ``team``, so it must be what runs), its agents' models
are priced, it passes the §3.4 checks and L2-9 (no scorer text in a prompt or the task),
and its dry-run gives every target a value on each of its scorers.

**Keys.** The code is checked without provider keys, but live epochs of a written
scenario run it in the per-epoch subprocess, which has the OpenRouter key (the agents
need it), and the L2 process imports it to build variants. Code that called a model
outside Inspect would not be counted by the ledger. Accepted until scenario code runs in
a sandbox (spec §12).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from inspect_ai.log import EvalLog
from inspect_ai.model import Model, ModelOutput, get_model
from inspect_ai.tool import ToolInfo
from inspect_ai.util import JSONSchema

from experiments.l2.model import InnerSpec, Variant
from experiments.scenarios import SCENARIOS, Scenario, build_team

Factory = Callable[[dict], Scenario]


@dataclass(frozen=True)
class Written:
    """A written scenario: its factory and the SHA-256 of the code that defines it."""

    make: Factory
    sha256: str


WrittenScenarios = Mapping[str, Written]
"""Name -> written scenario, as ``load`` returns them."""
NONE: WrittenScenarios = MappingProxyType({})
"""No written scenarios (the default wherever they may be passed)."""


def sha256(code: str | bytes) -> str:
    return hashlib.sha256(code.encode() if isinstance(code, str) else code).hexdigest()


def load_file(path: Path) -> Written:
    """Import one scenario module (under a module name of its own) and hash its code."""
    path = Path(path)
    name = f"l2_written_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses and annotations in it look itself up here
    spec.loader.exec_module(module)
    return Written(make=module.make, sha256=sha256(path.read_bytes()))


def load(directory: Path | None) -> dict[str, Written]:
    """Every written scenario in ``directory`` (none if it does not exist). The hash is
    of the code imported, not read from ``<name>.sha256``, which is for the record."""
    if directory is None or not Path(directory).is_dir():
        return {}
    return {path.stem: load_file(path) for path in sorted(Path(directory).glob("*.py"))}


def scenario(name: str, written: WrittenScenarios) -> Factory:
    """The factory of the scenario ``name``: a seed scenario, else a written one."""
    if name in SCENARIOS:
        return SCENARIOS[name]
    if name in written:
        return written[name].make
    raise KeyError(f"unknown scenario {name!r}")


def known(name: str, written: WrittenScenarios) -> bool:
    return name in SCENARIOS or name in written


# ------------------------------------------------------------- generic mocks
CHANNEL_TOOLS = ("channel_post", "channel_read", "channel_list")
"""Orbit's channel tools, which every agent has: not the scenario's "other" tools."""
POST = "Status update on my part of the migration."


def placeholder(schema: JSONSchema) -> Any:
    """A placeholder value for one parameter: its first enum value, else by type
    (``"x"``, ``1``, ``true``, ``[]``, an object with its required properties; a union's
    first non-null type)."""
    if schema.enum:
        return schema.enum[0]
    if schema.anyOf:
        return placeholder(next((s for s in schema.anyOf if s.type != "null"),
                                schema.anyOf[0]))
    kind = schema.type[0] if isinstance(schema.type, list) else schema.type
    if kind == "object":
        return {name: placeholder(schema.properties[name]) for name in schema.required or []}
    return {"integer": 1, "number": 1, "boolean": True, "array": []}.get(kind, "x")


def placeholders(info: ToolInfo) -> dict[str, Any]:
    """Placeholder arguments for a tool's required parameters."""
    return {name: placeholder(info.parameters.properties[name])
            for name in info.parameters.required}


def generic_models(scenario: Scenario) -> dict[str, Model]:
    """Mock models for the dry-run of ``scenario`` (built), for one sample (spec §3.5).

    Each agent, on each turn, makes one call: one of its other tools that no agent has
    called yet in the run, with placeholder arguments from the tool's schema; otherwise
    one short post on the first channel it can write to (from the setup, found by the
    agent's system prompt); otherwise its first other tool again; otherwise no call. So
    every other tool is called once by an agent that has it (the first agent to have an
    uncalled tool spends a turn on it instead of posting). The judge and probe answer a
    probe question with its first option, a yes/no question with ``NO``, anything else
    with ``x``."""
    channels = scenario.task["communication"].channels
    writable = {spec.system_prompt: [c.id for c in channels if spec.name in c.writers]
                for spec in scenario.task["setup"].agents}
    first_option = {p.question: p.options[0] for p in scenario.probes.values()}
    called: set[str] = set()

    def call(info: ToolInfo) -> ModelOutput:
        called.add(info.name)
        return ModelOutput.for_tool_call("mockllm", info.name, placeholders(info))

    def agent(messages, tools, tool_choice, config) -> ModelOutput:
        system = next((m.text for m in messages if m.role == "system"), "")
        mine = max((p for p in writable if p in system), key=len, default=None)
        if mine is None:  # never guess: an agent that cannot be found would never post
            raise ValueError("generic mock: no agent of the setup has this system prompt")
        others = [t for t in tools if t.name not in CHANNEL_TOOLS]
        if fresh := [t for t in others if t.name not in called]:
            return call(fresh[0])
        if writable[mine]:
            return ModelOutput.for_tool_call("mockllm", "channel_post",
                                             {"channel_id": writable[mine][0], "message": POST})
        if others:
            return call(others[0])
        return ModelOutput.from_content("mockllm", "Nothing to do.")

    def judge(messages, tools, tool_choice, config) -> ModelOutput:
        text = messages[-1].text
        answer = next((option for question, option in first_option.items()
                       if question in text), None)
        if answer is None:
            answer = "NO" if "Answer YES or NO" in text else "x"
        return ModelOutput.from_content("mockllm", answer)

    judge_model = get_model("mockllm/judge", custom_outputs=judge, memoize=False)
    return {"target": get_model("mockllm/model", custom_outputs=agent, memoize=False),
            "judge": judge_model, "probe": judge_model}


def check_variant(name: str) -> Variant:
    """The variant a written scenario is checked and dry-run as: itself, unchanged."""
    return Variant(id="check", base=name, mutations=(), inner=InnerSpec(condition="baseline"),
                   rationale="check")


def dry_run(built: Scenario, log_dir: Path) -> EvalLog:
    """One ``baseline`` sample of ``built`` (a written scenario built as its check
    variant, on the mock models) on the generic mocks, at no cost."""
    from inspect_ai import eval as inspect_eval

    from experiments.run import MAX_TOKENS, build_task, l2_metadata

    models = generic_models(built)
    task = build_task(built, "baseline", models,
                      metadata=l2_metadata(built, "baseline", epoch=1))
    [log] = inspect_eval(task, model=models["target"], log_dir=str(log_dir), display="none",
                         fail_on_error=False, score_on_error=True, max_tokens=MAX_TOKENS,
                         model_roles={"judge": models["judge"], "probe": models["probe"]})
    return log


# ------------------------------------------------------------------ checking
CHECK_TIMEOUT = 300
"""Seconds the check subprocess may take (it imports Inspect and runs one mock sample)."""


class CheckFailed(Exception):
    """The written scenario failed its checks; the message says why."""


def leaks(scenario: Scenario) -> list[str]:
    """L2-9: scorer text the agents would see, i.e. a probe question, the judge question
    or a free probe that is part of an agent's system prompt or of the task."""
    scorer_texts = [("probe question", p.question) for p in scenario.probes.values()]
    scorer_texts += [("judge question", scenario.judge)]
    scorer_texts += [(f"free probe {label!r}", q) for label, q in scenario.free_probes.items()]
    visible = {f"{spec.name}'s system prompt": spec.system_prompt or ""
               for spec in scenario.task["setup"].agents}
    visible["the task"] = scenario.task["dataset"][0].input
    return list(dict.fromkeys(
        f"the {kind} is in {where}" for kind, text in scorer_texts if text.strip()
        for where, seen in visible.items() if text.strip() in seen))


def unscored(log: EvalLog, built: Scenario) -> list[str]:
    """What the dry-run's scorers left without a value: a score that is missing or
    incomplete, or a target with no value (or a probe answer that is none of its options)."""
    from experiments.l2.results import VALUES, sample_results
    from experiments.scorers import expected_scores

    samples = log.samples or []
    if len(samples) != 1:
        return [f"the dry-run left {len(samples)} samples, not 1 (log status {log.status})"]
    if samples[0].error is not None:
        return [f"the dry-run errored: {samples[0].error.message}"]
    scores = samples[0].scores or {}
    expected = expected_scores(built)
    found = [f"{name}: no score" if name not in scores else
             f"{name}: {scores[name].metadata['incomplete']}" for name in expected
             if name not in scores or "incomplete" in (scores[name].metadata or {})]
    if found:
        return found
    [sample] = sample_results(log.location)
    for agent in built.targets:
        if built.outcome is not None and (
                scores["post_judge"].metadata["agents"][agent]["outcome"] is None):
            found.append(f"outcome: no value for {agent}")
        for name, value in VALUES.items():
            if name not in expected or (name == "counts" and built.mention is None) or (
                    name == "probe" and agent not in built.probes):
                continue
            got = value(sample, agent)
            if got is None:
                found.append(f"{name}: no value for {agent}")
            elif name == "probe" and got not in built.probes[agent].options:
                found.append(f"probe: {agent}'s answer {got!r} is none of its options "
                             f"{list(built.probes[agent].options)} (answers are read in "
                             "upper case)")
        if "free_probes" in expected:
            meta = scores["free_probes"].metadata
            answers = meta["branches"][meta["final"]]["agents"].get(agent, {}).get("answers")
            if missing := sorted(set(built.free_probes) - set(answers or {})):
                found.append(f"free_probes: no answer from {agent} to {missing}")
    return found


def check(path: Path, name: str) -> str:
    """Check the scenario module at ``path`` as ``name``; the warnings and its description,
    or ``CheckFailed`` with the reasons."""
    from experiments.l2.cost import PRICES
    from experiments.l2.describe import describe_scenario
    from experiments.l2.experiment import build

    try:
        found = {name: load_file(path)}
        scenario = found[name].make({})
    except (Exception, SystemExit) as error:
        raise CheckFailed(f"the code does not build: {type(error).__name__}: {error}") from error
    if not isinstance(scenario, Scenario):
        raise CheckFailed(f"make({{}}) must return a Scenario, not {type(scenario).__name__}")
    if (scenario.name, scenario.base) != (name, name):
        raise CheckFailed(f"make({{}}) must return a Scenario with name and base {name!r}, "
                          f"got name {scenario.name!r} and base {scenario.base!r}")
    if scenario.team is None:
        raise CheckFailed("the Scenario has no team: build its task with team_setup and "
                          "build_team and pass team=<the TeamSetup>, so ready-made changes "
                          "apply to it")
    if not same_task(scenario.task, build_team(name, scenario.team)):
        raise CheckFailed(f"the Scenario's task is not build_team({name!r}, team): runs "
                          "rebuild the task from team, so build it from the same TeamSetup")
    if unpriced := sorted({spec.model for spec in scenario.task["setup"].agents
                           if spec.model and spec.model not in PRICES}):
        raise CheckFailed(f"models not in the price table: {unpriced} (priced: "
                          f"{sorted(PRICES)})")
    _, built, problems = build(check_variant(name), mock=True, written=found)
    rejections = [*problems.rejections, *(leaks(built) if built is not None else [])]
    if rejections:
        raise CheckFailed("\n".join(f"- {reason}" for reason in rejections))
    with tempfile.TemporaryDirectory() as logs:
        try:
            missing = unscored(dry_run(built, Path(logs)), built)
        except Exception as error:  # e.g. a scorer that cannot be built for this setup
            missing = [f"{type(error).__name__}: {error}"]
        if missing:
            raise CheckFailed("the dry-run on the generic mocks failed:\n"
                              + "\n".join(f"- {reason}" for reason in missing))
    warnings = [f"Warning: {w}" for w in problems.warnings]
    return "\n".join([*warnings, *([""] if warnings else []),
                      describe_scenario(name, mock=False, written=found)])


def same_task(task: dict[str, Any], rebuilt: dict[str, Any]) -> bool:
    """``task`` is ``rebuilt``: equal but for the tool factories (closures), which must
    bind the same tools."""
    def plain(kwargs: dict[str, Any]) -> dict[str, Any]:
        return {**kwargs, "tool_bindings": sorted(kwargs.get("tool_bindings", {}))}

    return plain(task) == plain(rebuilt)


def check_code(name: str, code: str) -> tuple[bool, str]:
    """Run ``check`` on ``code`` in a subprocess without provider keys (every
    ``*_API_KEY`` set to empty, as in ``workspace.run_python``), from the repository so
    it can import its modules. ``(passed, the result or the reasons)``."""
    from experiments.l2 import workspace
    from experiments.l2.experiment import REPO

    env = {**os.environ, **{key: "" for key in workspace.key_names(REPO)}}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"{name}.py"
        path.write_text(code, encoding="utf-8")
        command = [sys.executable, "-m", "experiments.l2.written", "--check", str(path),
                   "--name", name]
        try:
            done = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True,
                                  timeout=CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"the check did not finish in {CHECK_TIMEOUT} s"
    if done.returncode == 0:
        return True, done.stdout.strip()
    return False, ("\n".join(part for part in (done.stdout.strip(), done.stderr.strip()[-3000:])
                             if part) or f"the check exited with code {done.returncode}")


def save(directory: Path, name: str, code: str) -> None:
    """``<name>.py`` and ``<name>.sha256`` in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(code, encoding="utf-8")  # as sha256 hashes it
    (directory / f"{name}.sha256").write_text(sha256(code) + "\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Check a scenario module L2 wrote.")
    parser.add_argument("--check", required=True, type=Path, metavar="FILE")
    parser.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    try:
        print(check(args.check, args.name))
    except CheckFailed as failure:
        print(failure)
        raise SystemExit(1) from None


if __name__ == "__main__":
    # The module the rest of the code imports, not a second copy run as __main__.
    from experiments.l2.written import main as _main

    _main()
