"""L2's code workspace (spec ``docs/level2-interface.md`` §6; plan
``docs/level2-milestone3.md``, "Code workspace"; test L2-15).

``run_python(code, ...)`` runs L2's Python in a subprocess, with the audit directory as
its working directory, a time limit and a cap on the output it returns. The subprocess
has no provider keys: every ``*_API_KEY`` in this environment, and every one named in a
``.env`` file Inspect would load from the workspace (it searches upward from the working
directory), is set to the empty string. Empty, not removed: ``python-dotenv`` never
overwrites a variable that is already set, so a ``.env`` loaded inside the workspace
cannot put a key back, and a model call made from it fails. Nothing more until the
workspace moves to an Inspect sandbox (spec §12): the code can still read any file the
user can, the repository's own ``.env`` included, so the "files" half of L2-15 waits
for the sandbox.

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


def run_python(code: str, cwd: Path, sources: Sequence[Path]) -> str:
    """Run ``code`` with ``python -I`` in ``cwd``; its exit code and (capped) output.
    ``L2_SOURCES`` holds the directories the audit's runs are read from. The code runs
    in a process group of its own, killed at the end, so nothing it starts outlives it."""
    cwd, time_limit = Path(cwd).resolve(), TIME_LIMIT
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
    if len(output) > OUTPUT_CHARS:
        output = (output[:OUTPUT_CHARS]
                  + f"\n… [{len(output) - OUTPUT_CHARS} more characters cut: print less]")
    return f"{head}\n{output}" if output else f"{head} (no output)"


def _kill(process: subprocess.Popen) -> None:
    """Kill the code's whole process group (anything it started too), if any is left."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
