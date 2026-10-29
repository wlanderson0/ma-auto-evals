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
