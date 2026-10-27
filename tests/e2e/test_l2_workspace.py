"""L2's code workspace, ``run_python`` (plan milestone 3 step 3; spec §6; L2-15)."""

from __future__ import annotations

import json

import pytest

import experiments.l2.workspace as workspace
from experiments.l2.audit import Audit
from experiments.l2.tools import TRACE_CHARS, Tools
from tests.support.l2_audit import InProcess, brief

C0 = {"id": "C0", "control": "M0", "rationale": "the control"}


def new(tmp_path, budget=1.0):
    audit = Audit.create(brief(tmp_path, budget), tmp_path / "audit", mock=True,
                         launch=InProcess())
    return Tools(audit)


def test_no_provider_key_is_visible_and_a_model_call_fails(tmp_path, monkeypatch):
    """L2-15, environment half: keys in L2's environment and in a .env above the
    workspace (which Inspect's eval() would load) are empty in it, and model calls from
    it fail. The files half (the code can still open the repository's .env) waits for
    the sandbox (spec §12)."""
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)  # as in a real audit
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-secret")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-dotenv-secret\n")
    tools = new(tmp_path)
    code = """
import json, os
from inspect_ai._util.dotenv import init_dotenv
init_dotenv()  # what inspect_ai.eval does first: load the .env found above the cwd
print(json.dumps({k: v for k, v in os.environ.items() if k.endswith("_API_KEY")}))
from inspect_ai.model import get_model
for model in ("openrouter/openai/gpt-4o-mini", "openai/gpt-4o-mini"):
    try:
        get_model(model)
        print("made", model)
    except Exception as error:
        print("failed", model, type(error).__name__)
"""
    ok, text = tools.call("run_python", code=code)
    assert ok and text.startswith("exit code 0"), text
    keys = json.loads(text.splitlines()[1])
    assert {"OPENAI_API_KEY", "OPENROUTER_API_KEY"} <= set(keys)
    assert set(keys.values()) == {""}
    assert "failed openrouter/openai/gpt-4o-mini" in text
    assert "failed openai/gpt-4o-mini" in text and "made" not in text


def test_without_blanking_inspect_would_load_the_key(tmp_path, monkeypatch):
    """Why the keys are set empty rather than removed."""
    import subprocess
    import sys

    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-dotenv-secret\n")
    code = ("from inspect_ai._util.dotenv import init_dotenv; init_dotenv(); "
            "import os; print(os.environ.get('OPENAI_API_KEY'))")
    done = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path,
                          capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "sk-dotenv-secret"


def test_nothing_the_code_starts_outlives_it(tmp_path, monkeypatch):
    import time

    tools = new(tmp_path)
    marker = tmp_path / "still-running"
    monkeypatch.setattr(workspace, "TIME_LIMIT", 2)
    code = f"""
import subprocess, sys
subprocess.Popen([sys.executable, "-c",
                  "import time; time.sleep(3); open({str(marker)!r}, 'w').write('x')"],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("started", flush=True)
"""
    ok, text = tools.call("run_python", code=code)
    assert ok and text.startswith("exit code 0") and "started" in text
    time.sleep(4)
    assert not marker.exists()


def test_output_that_is_not_utf8_is_still_returned(tmp_path):
    ok, text = new(tmp_path).call(
        "run_python", code="import sys; sys.stdout.buffer.write(b'ok \\xff')")
    assert ok and text.startswith("exit code 0\nok "), text


def test_code_reads_the_audits_logs_with_inspect_and_pandas(tmp_path, offline_module):
    tools = new(tmp_path)
    assert tools.call("run_experiment", variants=[C0], epochs=1)[0]
    code = """
import json, os
from pathlib import Path
import pandas as pd
from inspect_ai.log import read_eval_log
[source] = os.environ["L2_SOURCES"].split(os.pathsep)
rows = []
for line in (Path("experiments") / "exp-1" / "runs.jsonl").read_text().splitlines():
    run = json.loads(line)
    for path in run["logs"]:
        log = read_eval_log(str(Path("experiments") / "exp-1" / path))
        rows.append({"variant": run["variant"]["id"], "samples": len(log.samples)})
print(pd.DataFrame(rows).to_string(index=False))
print(Path.cwd().name, Path(source).name)
"""
    ok, text = tools.call("run_python", code=code)
    assert ok and text.startswith("exit code 0"), text
    assert "C0        1" in text and "audit exp-1" in text


def test_a_failing_or_slow_run_returns_what_happened(tmp_path, monkeypatch):
    tools = new(tmp_path)
    ok, text = tools.call("run_python", code="raise SystemExit('nope')")
    assert ok and text == "exit code 1\nnope\n"
    monkeypatch.setattr(workspace, "TIME_LIMIT", 1)
    ok, text = tools.call("run_python", code="import time\nprint('started', flush=True)\n"
                                              "time.sleep(10)")
    assert ok and text.startswith("stopped after the 1 s time limit") and "started" in text
    ok, text = tools.call("run_python", code="  ")
    assert not ok and "code is required" in text


def test_long_output_is_cut_and_the_trace_keeps_the_code_and_all_l2_saw(tmp_path):
    tools = new(tmp_path)
    code = "print('x' * 10000)"
    ok, text = tools.call("run_python", code=code)
    assert ok and "more characters cut" in text and len(text) < TRACE_CHARS
    row = json.loads((tools.audit.out / "trace.jsonl").read_text().splitlines()[-1])
    assert (row["tool"], row["args"], row["result"]) == ("run_python", {"code": code}, text)


def test_code_runs_in_an_analysis_audit_but_not_after_the_hard_stop(tmp_path):
    source = tmp_path / "m1"
    source.mkdir()
    (source / "runs.jsonl").write_text("")
    analysis = brief(tmp_path, 1.0)
    tools = Tools(Audit.create(analysis, tmp_path / "audit", mock=True, analysis=source))
    ok, text = tools.call("run_python",
                          code="import os; print(os.environ['L2_SOURCES'].endswith('m1'))")
    assert ok and "True" in text
    tools.audit.set_l2_spend({"usd": 0.95})
    ok, text = tools.call("run_python", code="print(1)")
    assert not ok and "hard stop" in text


@pytest.mark.parametrize("name", ["OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"])
def test_key_names_cover_the_environment_and_dotenv_files_above(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, "x")
    (tmp_path / ".env").write_text("MISTRAL_API_KEY=y\nINSPECT_LOG_DIR=logs\n")
    (tmp_path / "a" / "b").mkdir(parents=True)
    found = workspace.key_names(tmp_path / "a" / "b")
    assert {name, "MISTRAL_API_KEY"} <= found and "INSPECT_LOG_DIR" not in found


def test_the_tool_description_states_the_limits():
    from inspect_ai.tool import ToolDef

    from experiments.l2.tools import inspect_tools

    class Stub:  # inspect_tools only wraps; nothing is called here
        audit = None

    [run] = [ToolDef(t) for t in inspect_tools(Stub()) if ToolDef(t).name == "run_python"]
    assert f"after {workspace.TIME_LIMIT} seconds" in " ".join(run.description.split())
    assert f"past {workspace.OUTPUT_CHARS} characters" in " ".join(run.description.split())
