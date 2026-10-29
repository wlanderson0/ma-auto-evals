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

**Keys.** The code is checked without provider keys, but live epochs of a written
scenario run it in the per-epoch subprocess, which has the OpenRouter key (the agents
need it). Code that called a model outside Inspect would not be counted by the ledger.
Accepted until scenario code runs in a sandbox (spec §12).
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
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
from experiments.scenarios import SCENARIOS, Scenario

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


def dry_run(name: str, written: WrittenScenarios, log_dir: Path) -> EvalLog:
    """One ``baseline`` sample of the written scenario ``name`` on the generic mocks,
    built as a variant of it is (``experiment.build``), at no cost."""
    # Imported here: experiment imports this module.
    from inspect_ai import eval as inspect_eval

    from experiments.l2.experiment import build
    from experiments.run import MAX_TOKENS, build_task, l2_metadata

    _, built, problems = build(check_variant(name), mock=True, written=written)
    if built is None or problems.rejected:
        raise ValueError(f"{name} is rejected: {'; '.join(problems.rejections)}")
    models = generic_models(built)
    task = build_task(built, "baseline", models,
                      metadata=l2_metadata(built, "baseline", epoch=1))
    [log] = inspect_eval(task, model=models["target"], log_dir=str(log_dir), display="none",
                         fail_on_error=False, score_on_error=True, max_tokens=MAX_TOKENS,
                         model_roles={"judge": models["judge"], "probe": models["probe"]})
    return log
