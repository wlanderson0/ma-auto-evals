"""L2's sandbox, for real (``experiments.l2.sandbox``; spec ``docs/level2-interface.md``
§6, §12, contract test L2-26; Cosmos spec 20, R18): the check's properties,
``run_python`` and the scenario check inside it, and the tool refused, never run outside,
when it cannot start.

Tests that need the platform's backend (bwrap on Linux, sandbox-exec on macOS) are
skipped when it cannot start here, unless ``L2_SANDBOX_TESTS=require`` is set: then a
sandbox that cannot start fails them (the sandbox workflow sets it)."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import experiments.l2.workspace as workspace
from experiments.l2 import sandbox as sandbox_
from experiments.l2 import written as written_
from experiments.l2.audit import Audit
from experiments.l2.experiment import REPO
from experiments.l2.sandbox import BOOTSTRAP, PROPERTIES, Check, Sandbox, SandboxFailed
from experiments.l2.tools import Tools
from experiments.scenarios import SCENARIOS
from tests.e2e.test_l2_write_scenario import REFUSED
from tests.support.l2_audit import InProcess, brief, fixed
from tests.support.written import RUMOUR

C0 = {"id": "C0", "control": "M0", "rationale": "the control"}
REQUIRED = os.environ.get("L2_SANDBOX_TESTS") == "require"
FALSE = shutil.which("false") or "/bin/false"
"""A program that starts and fails (``/usr/bin/false`` on macOS)."""


@pytest.fixture(scope="module")
def box(tmp_path_factory) -> Sandbox:
    """The platform's sandbox, once it has run a line of Python here."""
    try:
        found = sandbox_.detect()
        folder = tmp_path_factory.mktemp("start")
        ran = found.run(["-I", "-c", "print('up')"], cwd=folder, reads=[], write=folder,
                        timeout=60)
        assert ran.stdout.strip() == "up", ran
    except (SandboxFailed, AssertionError) as error:
        if REQUIRED:
            pytest.fail(f"L2_SANDBOX_TESTS=require, but the sandbox cannot start: {error}")
        pytest.skip(f"the sandbox cannot start here: {error}")
    return found


def new(tmp_path, sandbox=None, budget=1.0, **options):
    options.setdefault("launch", InProcess())
    tmp_path.mkdir(parents=True, exist_ok=True)
    audit = Audit.create(brief(tmp_path, budget), tmp_path / "audit", mock=True,
                         sandbox=sandbox, **options)
    return Tools(audit)


class Outside(Sandbox):
    """A "sandbox" that runs the command outside, with the bootstrap or a stand-in: to
    show that the check catches a sandbox that does not hold, and how a run that never
    sends the ready byte is told apart from code that fails."""

    def __init__(self, bootstrap: str = BOOTSTRAP, chdir: bool = True) -> None:
        super().__init__("bwrap", sys.executable)
        object.__setattr__(self, "bootstrap", bootstrap)
        object.__setattr__(self, "chdir", chdir)

    def command(self, run, args, ready, scratch):
        inherited = [f"{name}={value}" for name, value in os.environ.items()]
        there = ["/bin/sh", "-c", 'cd "$0" && exec "$@"', run.cwd] if self.chdir else []
        return ["/usr/bin/env", *inherited, *there, run.python, "-I", "-c", self.bootstrap,
                str(ready), run.python, *args]


# ------------------------------------------------------------------ check
@pytest.mark.enable_socket
def test_every_property_of_the_check_holds(box):
    found = sandbox_.check(box)
    assert found.kind == box.kind
    assert list(found.properties) == list(PROPERTIES), found.properties
    assert found.passed, found.failed()


@pytest.mark.enable_socket
def test_the_check_fails_a_sandbox_that_does_not_hold(monkeypatch):
    monkeypatch.setattr(sandbox_, "OUTBOUND", "127.0.0.1")  # no address outside, in a test
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    found = sandbox_.check(Outside())
    assert not found.passed
    held = {name: ok for name, (ok, _) in found.properties.items()}
    assert held["starts"] and held["reads-sources"] and held["writes-inside-work"]
    for name in ("no-local-tcp", "no-udp", "hidden-files", "writes-outside-fail",
                 "clean-environment"):
        assert not held[name], (name, found.properties[name])
    assert "OPENROUTER_API_KEY" in found.properties["clean-environment"][1]
    assert any(line.startswith("hidden-files: visible:") for line in found.failed())


@pytest.mark.enable_socket
def test_the_check_fails_a_sandbox_whose_code_is_not_in_the_audit_directory(monkeypatch):
    """Code that cannot tell where it is (``getcwd``), or is somewhere else, has not
    started as an audit's code does."""
    monkeypatch.setattr(sandbox_, "OUTBOUND", "127.0.0.1")
    found = sandbox_.check(Outside(chdir=False))
    held, detail = found.properties["starts"]
    assert not held and detail == "the probe ran, but not in the audit directory: /"


@pytest.mark.enable_socket
def test_the_check_names_a_sandbox_that_cannot_start(tmp_path):
    found = sandbox_.check(Sandbox("bwrap", str(tmp_path / "no-bwrap")))
    assert not found.passed and list(found.properties) == ["starts"]
    assert "could not be started" in found.properties["starts"][1]


def check_command(*args, env=None):
    return subprocess.run([sys.executable, "-m", "experiments.l2.sandbox", *args], cwd=REPO,
                          capture_output=True, text=True, timeout=120, env=env)


def test_the_check_command_exits_0_with_one_line_naming_the_kind(box):
    done = check_command("--check")
    assert (done.returncode, done.stdout) == (0, f"sandbox ok: {box.kind}\n"), done.stderr
    done = check_command("--check", "--json")
    found = json.loads(done.stdout)
    assert done.returncode == 0 and found["passed"] and found["kind"] == box.kind
    assert set(found["properties"]) == set(PROPERTIES)


def test_the_check_command_exits_1_with_a_line_per_failed_property(monkeypatch, capsys):
    failing = Check("bwrap", {**{name: (True, "fine") for name in PROPERTIES},
                              "no-udp": (False, "a datagram arrived"),
                              "hidden-files": (False, "visible: ['/home/x/.env']")})
    monkeypatch.setattr(sandbox_, "check", lambda: failing)
    with pytest.raises(SystemExit) as stopped:
        sandbox_.main(["--check"])
    assert stopped.value.code == 1
    assert capsys.readouterr().out.splitlines() == [
        "failed: no-udp: a datagram arrived",
        "failed: hidden-files: visible: ['/home/x/.env']"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bwrap is found on PATH")
def test_the_check_command_exits_1_when_there_is_no_bwrap():
    env = {**os.environ, "PATH": "/nonexistent"}
    done = check_command("--check", env=env)
    assert done.returncode == 1
    assert done.stdout.startswith("failed: starts: bwrap (bubblewrap) is not installed")


@pytest.mark.parametrize("args", [[], ["--check", "--bogus"], ["--json"]])
def test_the_check_command_exits_2_for_bad_arguments(args):
    done = check_command(*args)
    assert done.returncode == 2 and not done.stdout


# --------------------------------------------------------- starting or not
def test_no_ready_byte_means_the_sandbox_did_not_start(tmp_path):
    marker = tmp_path / "ran"
    silent = Outside(bootstrap="import sys; sys.stderr.write('bwrap: setting up uid map: "
                               "Permission denied\\n'); sys.exit(1)")
    with pytest.raises(SandboxFailed, match="did not start, so nothing ran: bwrap: setting up"):
        silent.run(["-I", "-c", f"open({str(marker)!r}, 'w')"], cwd=tmp_path, reads=[],
                   write=tmp_path, timeout=30)
    assert not marker.exists()


def test_code_that_fails_after_the_ready_byte_is_code_that_failed(tmp_path):
    ran = Outside().run(["-I", "-c", "raise SystemExit('nope')"], cwd=tmp_path, reads=[],
                        write=tmp_path, timeout=30, merge=True)
    assert (ran.returncode, ran.stdout, ran.timed_out) == (1, "nope\n", False)


@pytest.mark.parametrize("kind", ["bwrap", "sandbox-exec"])
@pytest.mark.parametrize("binary", ["missing", "false"])
def test_a_sandbox_that_cannot_start_refuses_run_python_and_runs_nothing(tmp_path, kind,
                                                                         binary):
    path = str(tmp_path / "no-such-binary") if binary == "missing" else FALSE
    tools = new(tmp_path, sandbox=Sandbox(kind, path))
    marker = tmp_path / "ran-outside"
    ok, text = tools.call("run_python", code=f"open({str(marker)!r}, 'w').write('x')")
    assert not ok and text.startswith("refused: ") and "nothing ran" in text, text
    assert not marker.exists()


def test_a_sandbox_that_cannot_start_refuses_write_scenario_and_saves_nothing(tmp_path):
    marker = tmp_path / "checked-outside"
    code = RUMOUR.replace("def make(models):\n", (
        f"open({str(marker)!r}, 'w').write('x')\n\n\ndef make(models):\n"))
    tools = new(tmp_path, sandbox=Sandbox("bwrap", FALSE))
    ok, text = tools.call("write_scenario", name="rumour", code=code)
    assert not ok and "did not start" in text and "Nothing was saved" in text, text
    assert not marker.exists() and not (tools.audit.out / "scenarios").exists()


# ------------------------------------------------------------- run_python
def test_run_python_reads_the_runs_and_saves_a_figure_but_nothing_else(box, tmp_path,
                                                                       offline_module):
    tools = new(tmp_path, sandbox=box)
    assert tools.call("run_experiment", variants=[C0], epochs=1, rationale="a test launch")[0]
    out = tools.audit.out
    (out / "l2").mkdir()
    (out / "l2" / "secret.txt").write_text("L2's own log")
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-canary\n")
    code = f"""
import json, os, socket
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
from inspect_ai.log import read_eval_log
[source] = os.environ["L2_SOURCES"].split(os.pathsep)
rows = []
for line in (Path(source) / "runs.jsonl").read_text().splitlines():
    run = json.loads(line)
    for path in run["logs"]:
        log = read_eval_log(str(Path(source) / path))
        rows.append({{"variant": run["variant"]["id"], "samples": len(log.samples)}})
frame = pd.DataFrame(rows)
print(frame.to_string(index=False))
plt.bar(frame["variant"], frame["samples"])
plt.savefig("figures/samples.png")
for path in ["brief.md", "ledger.json", "l2/secret.txt", {str(tmp_path / '.env')!r},
             {str(REPO / 'pyproject.toml')!r}]:
    try:
        open(path).read()
        print("read", path)
    except OSError as error:
        print("hidden", path)
for path in ["note.txt", {str(tmp_path / 'outside.txt')!r}]:
    try:
        open(path, "w").write("x")
        print("wrote", path)
    except OSError:
        print("refused", path)
try:
    socket.create_connection(("127.0.0.1", 9), timeout=2)
except OSError as error:
    print("no network")
print(sorted(k for k in os.environ if k.endswith("_API_KEY")))
"""
    ok, text = tools.call("run_python", code=code)
    assert ok and text.startswith("exit code 0"), text
    assert "C0        1" in text
    assert (out / "figures" / "samples.png").read_bytes().startswith(b"\x89PNG")
    assert "read " not in text and text.count("hidden ") == 5, text
    assert "wrote" not in text and text.count("refused ") == 2, text
    assert not (out / "note.txt").exists() and not (tmp_path / "outside.txt").exists()
    assert "no network" in text and "[]" in text


def test_run_python_in_the_sandbox_keeps_its_time_limit_and_kills_what_it_started(
        box, tmp_path, monkeypatch):
    tools = new(tmp_path, sandbox=box)
    marker = tools.audit.out / "figures" / "still-running"
    monkeypatch.setattr(workspace, "TIME_LIMIT", 2)
    code = f"""
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c",
                  "import time; time.sleep(3); open({str(marker)!r}, 'w').write('x')"],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
print("started", flush=True)
time.sleep(30)
"""
    started = time.monotonic()
    ok, text = tools.call("run_python", code=code)
    assert ok and time.monotonic() - started < 15
    if box.kind == "sandbox-exec":  # no pid namespace: the code may start no process
        assert text.startswith("exit code 1") and "PermissionError" in text, text
    else:
        assert text.startswith("stopped after the 2 s time limit") and "started" in text
    time.sleep(4)
    assert not marker.exists()


def test_run_python_under_sandbox_exec_starts_no_process(box, tmp_path):
    """macOS has no pid namespace: a process that left the group would outlive the run,
    so the code can start none, by any call."""
    if box.kind != "sandbox-exec":
        pytest.skip("bwrap's pid namespace ends with the run, so processes are allowed")
    code = """
import os, subprocess, sys
for name, start in [
        ("fork", os.fork),
        ("spawn", lambda: os.posix_spawn(sys.executable, [sys.executable, "-c", ""], {})),
        ("popen", lambda: subprocess.Popen([sys.executable, "-c", ""]))]:
    try:
        pid = start()
        if pid == 0:
            os._exit(0)
        print("started", name)
    except OSError as error:
        print("refused", name)
"""
    ok, text = new(tmp_path, sandbox=box).call("run_python", code=code)
    assert ok and "started" not in text and text.count("refused") == 3, text


def test_code_killed_by_a_signal_shows_the_signal_as_without_the_sandbox(box, tmp_path):
    code = "import os, signal; print('x', flush=True); os.kill(os.getpid(), signal.SIGKILL)"
    plain = new(tmp_path / "plain").call("run_python", code=code)[1]
    boxed = new(tmp_path / "boxed", sandbox=box).call("run_python", code=code)[1]
    assert plain.splitlines()[0] == boxed.splitlines()[0] == "exit code -9", (plain, boxed)
    exited = new(tmp_path / "exit", sandbox=box).call("run_python", code="raise SystemExit(3)")
    assert exited[1].startswith("exit code 3")


def test_run_python_output_is_capped_in_the_sandbox(box, tmp_path):
    ok, text = new(tmp_path, sandbox=box).call("run_python", code="print('x' * 10000)")
    assert ok and "more characters cut" in text


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
def test_what_the_code_plants_in_figures_is_removed_after_the_run(box, tmp_path):
    tools = new(tmp_path, sandbox=box)
    code = """
import os
os.symlink("/etc/passwd", "figures/V1-vs-C0-probe.png")
os.symlink("..", "figures/up")
os.mkfifo("figures/fifo")
open("figures/plot.png", "wb").write(b"png")
print(sorted(os.listdir("figures")))
"""
    ok, text = tools.call("run_python", code=code)
    assert ok and "'fifo'" in text, text
    figures = tools.audit.out / "figures"
    assert sorted(p.name for p in figures.iterdir()) == ["plot.png"]


HIDE = """
import json, os, sys
from pathlib import Path
from experiments.l2 import study, workspace
from experiments.l2.sandbox import detect
from experiments.l2.tools import embedded
audit = Path(sys.argv[1])
(audit / "l2").mkdir()
(audit / "l2" / "inbox.jsonl").write_text("not for the export")
code = (
    "import os\\n"
    "os.mkdir('figures/d')\\n"
    "os.symlink('../../l2/inbox.jsonl', 'figures/d/leak.png')\\n"
    "os.chmod('figures/d', 0o111)\\n"
    "os.chmod('figures', 0o111)\\n"
    "print('hidden')")
said = workspace.run_python(code, audit, [], detect())
(audit / "writeup.md").write_text("![leak](figures/d/leak.png)")
out = audit / "export"
out.mkdir()
study._copy_images(audit, out, print)
print(json.dumps({"said": said, "link": os.path.lexists(audit / "figures/d/leak.png"),
                  "embedded": str(embedded(audit, "figures/d/leak.png")),
                  "images": study.images(audit), "exported": sorted(
                      p.name for p in out.rglob("*") if p.is_file())}))
"""


def test_a_link_hidden_in_an_unlistable_directory_is_removed_and_never_carried(box,
                                                                              tmp_path):
    """Code hides a link to ``l2/`` in directories it made unlistable (mode 0o111): the
    link is still removed after the run, and the study export carries nothing. File modes
    bind only a user other than root, so as root the script runs as ``nobody``."""
    root = os.geteuid() == 0
    audit = Path(tempfile.mkdtemp(prefix="l2-hide-")) if root else tmp_path / "audit"
    try:
        audit.mkdir(exist_ok=True)
        user = {}
        if root:
            nobody = pwd.getpwnam("nobody")
            os.chown(audit, nobody.pw_uid, nobody.pw_gid)
            user = {"user": nobody.pw_uid, "group": nobody.pw_gid, "extra_groups": []}
        env = {"PATH": "/usr/bin:/bin", "HOME": str(audit), "PYTHONPATH": str(REPO),
               "PYTHON_DOTENV_DISABLED": "1", "ORBIT_AUTOFETCH": "0",
               "ORBIT_AUTOSTART_SERVICE": "0"}
        done = subprocess.run([sys.executable, "-c", HIDE, str(audit)], env=env, cwd=audit,
                              capture_output=True, text=True, timeout=110, **user)
        assert done.returncode == 0, done.stderr
        found = json.loads(done.stdout.splitlines()[-1])
        assert found["said"].startswith("exit code 0") and "hidden" in found["said"]
        assert not found["link"] and found["embedded"] == "None"
        assert found["images"] == [] and found["exported"] == []
    finally:
        if root:
            shutil.rmtree(audit, ignore_errors=True)


def test_the_tool_description_says_what_the_sandbox_allows(box, tmp_path):
    from inspect_ai.tool import ToolDef

    from experiments.l2.tools import inspect_tools

    def description(tools):
        [run] = [ToolDef(t) for t in inspect_tools(tools) if ToolDef(t).name == "run_python"]
        return " ".join(run.description.split())

    inside = description(new(tmp_path / "a", sandbox=box))
    assert "in a sandbox" in inside and "write only under figures/" in inside
    assert "no network" in inside and f"after {workspace.TIME_LIMIT} seconds" in inside
    assert "sandbox" not in description(new(tmp_path / "b"))


# -------------------------------------------------------------- the check
def test_a_written_scenario_passes_its_check_and_dry_run_in_the_sandbox(box, tmp_path):
    code = RUMOUR.replace("def make(models):\n", (
        "def make(models):\n    import os\n"
        "    try:\n"
        f"        open({str(REPO / 'pyproject.toml')!r}).close()\n"
        "    except OSError:\n"
        "        pass\n"
        "    else:\n"
        "        raise RuntimeError('the checkout is readable')\n"
        "    if any(k.endswith('_API_KEY') or k.startswith('COSMOS') for k in os.environ):\n"
        "        raise RuntimeError('a variable of the caller is visible')\n"))
    tools = new(tmp_path, sandbox=box, cost_of=fixed(0.01))
    ok, text = tools.call("write_scenario", name="rumour", code=code)
    assert ok, text
    assert "passed its checks and its dry-run" in text and "## rumour" in text
    assert (tools.audit.out / "scenarios" / "rumour.py").read_text() == code


CHECKED = sorted(case for case, (name, _, _) in REFUSED.items()
                 if name.isidentifier() and name not in SCENARIOS)
"""The cases of ``test_l2_write_scenario`` that reach the check (the others are refused
by the tool before it, by their name)."""


@pytest.mark.parametrize("case", CHECKED)
def test_a_bad_scenario_fails_its_check_in_the_sandbox_with_the_same_reason(box, case):
    name, code, reason = REFUSED[case]
    passed, text = written_.check_code(name, code, box)
    assert not passed and reason in text, text


def test_an_interpreter_reached_through_a_link_starts_in_the_sandbox(box, tmp_path):
    """uv's interpreters sit behind a link (``cpython-3.13-...`` to ``cpython-3.13.9-...``),
    and a virtualenv names its base by that link: the sandbox has to carry it."""
    os.symlink(sys.base_prefix, tmp_path / "base")
    python = tmp_path / "base" / "bin" / f"python{sys.version_info[0]}"
    subprocess.run([python, "-m", "venv", "--without-pip", tmp_path / "venv"], check=True)
    (tmp_path / "w").mkdir()
    code = ("import sys\nfrom pathlib import Path\nfrom experiments.l2 import sandbox\n"
            "ran = sandbox.detect().run(['-I', '-c', 'import sys, json; "
            "print(sys.prefix != sys.base_prefix)'], cwd=Path(sys.argv[1]), reads=[], "
            "write=Path(sys.argv[1]), timeout=60)\nprint(ran.returncode, ran.stdout.strip())\n")
    ran = subprocess.run([tmp_path / "venv" / "bin" / "python", "-c", code, tmp_path / "w"],
                         capture_output=True, text=True, timeout=110,
                         env={"PYTHONPATH": str(REPO), "PATH": os.environ["PATH"]})
    assert ran.stdout.strip() == "0 True", ran


# -------------------------------------------------------------- the audit
def test_sandbox_report_only_when_on_and_writing_no_scenarios(tmp_path):
    box = Sandbox("bwrap", "/usr/bin/bwrap")
    assert new(tmp_path / "off").audit.sandbox_report() is None
    assert new(tmp_path / "writes", sandbox=box).audit.sandbox_report() is None
    path = brief(tmp_path, 1.0, extra="\nallowed_changes: [topology]")
    audit = Audit.create(path, tmp_path / "no-scenarios" / "audit", mock=True, sandbox=box)
    assert not audit.writes_scenarios
    assert audit.sandbox_report() == {
        "kind": "bwrap", "network": False,
        "write": str((audit.out / "figures").resolve())}
    source = tmp_path / "m1"
    source.mkdir()
    (source / "runs.jsonl").write_text("")
    mac = Sandbox("sandbox-exec", "/usr/bin/sandbox-exec")
    (tmp_path / "an").mkdir()
    path = brief(tmp_path / "an", 1.0)
    analysis = Audit.create(path, tmp_path / "analysis", mock=True, analysis=source,
                            sandbox=mac)
    assert analysis.sandbox_report()["kind"] == "sandbox-exec"
    assert Audit.create(path, tmp_path / "analysis-off", mock=True,
                        analysis=source).sandbox_report() is None


def test_an_audit_that_imports_written_scenarios_reports_no_sandbox(tmp_path):
    """An analysis-only audit imports the analysed audit's scenarios into its own
    process: with one on disk it reports no sandbox, and nothing is imported to tell."""
    earlier = tmp_path / "earlier"
    source = earlier / "experiments" / "exp-1"
    source.mkdir(parents=True)
    (source / "runs.jsonl").write_text("")
    (earlier / "scenarios").mkdir()
    marker = tmp_path / "imported"
    (earlier / "scenarios" / "planted.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n")
    analysis = Audit.create(brief(tmp_path, 1.0), tmp_path / "analysis", mock=True,
                            analysis=source, sandbox=Sandbox("bwrap", "/usr/bin/bwrap"))
    assert analysis.holds_scenarios and not analysis.writes_scenarios
    assert analysis.sandbox_report() is None and not marker.exists()
    (earlier / "scenarios" / "planted.py").unlink()
    assert analysis.sandbox_report() is not None


@pytest.mark.parametrize("how", ["flag", "environment"])
def test_an_audit_with_a_sandbox_that_fails_its_check_does_not_start(tmp_path, monkeypatch,
                                                                     capsys, how):
    from experiments.l2 import audit as audit_

    failing = Check("bwrap", {"starts": (False, "bwrap: setting up uid map: denied")})
    monkeypatch.setattr(sandbox_, "check", lambda: failing)
    out = tmp_path / "audit"
    argv = ["--brief", str(brief(tmp_path, 1.0)), "--mock", "--fake", "--out", str(out),
            "--quiet"]
    if how == "flag":
        argv.append("--sandbox")
    else:
        monkeypatch.setenv("L2_SANDBOX", "1")
    with pytest.raises(SystemExit) as stopped:
        audit_.main(argv)
    assert stopped.value.code not in (0, None)
    assert str(stopped.value.code).splitlines() == [
        "the sandbox does not hold here, so the audit does not start:",
        "starts: bwrap: setting up uid map: denied",
        *(f"{name}: not checked" for name in PROPERTIES[1:])]
    assert not out.exists()


def test_an_audit_with_a_sandbox_that_holds_runs_its_tools_in_it(tmp_path, monkeypatch):
    from experiments.l2 import agent
    from experiments.l2 import audit as audit_

    box = Sandbox("bwrap", "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox_, "check",
                        lambda: Check("bwrap", {n: (True, "fine") for n in PROPERTIES}))
    monkeypatch.setattr(sandbox_, "detect", lambda: box)
    seen = []
    monkeypatch.setattr(agent, "run_l2", lambda tools, *args, **kwargs: seen.append(tools))
    monkeypatch.delenv("L2_SANDBOX", raising=False)
    with pytest.raises(SystemExit):  # the stand-in never ends the audit
        audit_.main(["--brief", str(brief(tmp_path, 1.0)), "--mock", "--fake", "--quiet",
                     "--out", str(tmp_path / "audit"), "--sandbox"])
    [tools] = seen
    assert tools.audit.sandbox is box
    seen.clear()
    with pytest.raises(SystemExit):
        audit_.main(["--brief", str(brief(tmp_path, 1.0)), "--mock", "--fake", "--quiet",
                     "--out", str(tmp_path / "plain")])
    assert seen[0].audit.sandbox is None
