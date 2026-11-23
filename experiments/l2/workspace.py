"""L2's code workspace (spec ``docs/level2-interface.md`` §6; plan
``docs/level2-milestone3.md``, "Code workspace"; test L2-15).

``run_python(code, ...)`` runs L2's Python in a subprocess, with the audit directory as
its working directory, a time limit and a cap on the output it returns.

**In the sandbox** (``sandbox``, when the audit has one: every audit that needs
``--live``, unless ``--no-sandbox`` is given, and a scripted audit with ``--sandbox`` or
``L2_SANDBOX=1``): the code has no network, reads only the interpreter's environment, the
directories of the audit's runs (``L2_SOURCES``) and ``figures/``, and writes only
``<audit>/figures``. Its environment is built from nothing, so it holds no key. Nothing
else of the home directory, the checkout or the audit is visible (no ``.env``, no
``l2/``). When the sandbox cannot start the tool is refused; the code never runs
outside it. After each run anything in ``figures/`` that is not a regular file or a
directory is removed, even inside a directory the code made unlistable, and the code
that later reads ``figures/`` never follows a link there out of it. Code killed by
signal ``N`` shows as ``exit code -N``, as without the sandbox. Under
sandbox-exec the code cannot start a process: macOS has no pid namespace, so one that
left the process group could outlive the time limit.

**Without the sandbox** (``--no-sandbox``, or a scripted audit that did not ask for
one) the subprocess has no provider keys: every
``*_API_KEY`` in this environment, and every one named in a ``.env`` file Inspect would
load from the workspace (it searches upward from the working directory), is set to the
empty string. Empty, not removed: ``python-dotenv`` never overwrites a variable that is
already set, so a ``.env`` loaded inside the workspace cannot put a key back, and a model
call made from it fails. Nothing more: the code can still read any file the user can,
the repository's own ``.env`` included, and use the network.

The code and its output are recorded with every other tool call in ``trace.jsonl``;
the output is capped below the trace's own limit, so the trace keeps all of it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values

from experiments.l2.figures import FIGURES
from experiments.l2.sandbox import Sandbox

TIME_LIMIT = 60
"""Seconds a run may take before it is stopped."""
OUTPUT_CHARS = 3500
"""Output beyond this many characters is cut; under ``tools.TRACE_CHARS``, so the trace
keeps all of what L2 saw."""
KEY_SUFFIX = "_API_KEY"


def key_names(cwd: Path) -> set[str]:
    """Provider key names in this environment or in a ``.env`` above ``cwd``."""
    names = {name for name in os.environ if name.endswith(KEY_SUFFIX)}
    for directory in (cwd, *cwd.parents):
        if (directory / ".env").is_file():
            names |= {n for n in dotenv_values(directory / ".env") if n.endswith(KEY_SUFFIX)}
    return names


def run_python(code: str, cwd: Path, sources: Sequence[Path],
               sandbox: Sandbox | None = None) -> str:
    """Run ``code`` with ``python -I`` in ``cwd``; its exit code and (capped) output.
    ``L2_SOURCES`` holds the directories the audit's runs are read from. The code runs
    in a process group of its own, killed at the end, so nothing it starts outlives it.
    With ``sandbox`` it runs there (``sandboxed``), and ``SandboxFailed`` means it did
    not run."""
    cwd, time_limit = Path(cwd).resolve(), TIME_LIMIT
    if sandbox is not None:
        return sandboxed(code, cwd, sources, sandbox)
    env = {**os.environ, **{name: "" for name in key_names(cwd)},
           "L2_SOURCES": os.pathsep.join(str(Path(s).resolve()) for s in sources)}
    process = subprocess.Popen([sys.executable, "-I", "-c", code], cwd=cwd, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, encoding="utf-8",
                               errors="replace", start_new_session=True)
    try:
        output, _ = process.communicate(timeout=time_limit)
        head = f"exit code {process.returncode}"
    except subprocess.TimeoutExpired:
        _kill(process)
        output, _ = process.communicate()
        head = f"stopped after the {time_limit} s time limit"
    finally:
        _kill(process)
    return _answer(head, output)


def sandboxed(code: str, cwd: Path, sources: Sequence[Path], sandbox: Sandbox) -> str:
    """``run_python`` in ``sandbox``: reads the sources and ``figures/``, writes only
    ``<cwd>/figures`` (made first). It starts no process under sandbox-exec, where one
    that left the process group could not be stopped (``Sandbox.run``)."""
    figures = Path(cwd) / FIGURES
    figures.mkdir(exist_ok=True)
    sources = [Path(s).resolve() for s in sources]
    ran = sandbox.run(["-I", "-B", "-c", code], cwd=cwd, reads=sources, write=figures,
                      extra={"L2_SOURCES": os.pathsep.join(map(str, sources))},
                      timeout=TIME_LIMIT, merge=True, children=False)
    head = (f"stopped after the {TIME_LIMIT} s time limit" if ran.timed_out
            else f"exit code {ran.returncode}")
    return _answer(head, ran.stdout)


def _answer(head: str, output: str) -> str:
    """What L2 reads: how the run ended, then its output, capped."""
    if len(output) > OUTPUT_CHARS:
        output = (output[:OUTPUT_CHARS]
                  + f"\n… [{len(output) - OUTPUT_CHARS} more characters cut: print less]")
    return f"{head}\n{output}" if output else f"{head} (no output)"


def _kill(process: subprocess.Popen) -> None:
    """Kill the code's whole process group (anything it started too), if any is left."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
