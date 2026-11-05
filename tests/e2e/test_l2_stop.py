"""Stopping an audit early, by hand or by time (spec §7, "Stopping early"; L2-23).

One mechanism: a stop (Ctrl-C, or the time limit's held-back share reached) means no new
experiment starts and L2 is told to conclude; the query tools still work, so it can read
what ran. The time limit, or a second Ctrl-C, also stops a running experiment before its
next epoch; a first Ctrl-C lets it finish. A second Ctrl-C stops the running epoch and L2
at once, and the record is still written. Offline; the clock is the audit's own, set by
hand.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest
from inspect_ai.log import read_eval_log, resolve_sample_attachments

import experiments.l2.experiment as m1
from experiments.l2.agent import interrupt_handler, run_l2
from experiments.l2.audit import TIME_RESERVE, Audit, main
from experiments.l2.audit_report import write_report
from experiments.l2.brief import BriefError, parse_brief
from experiments.l2.fake import PROMPT, fake_model
from experiments.l2.tools import Tools
from tests.support.l2_audit import InProcess, brief

C0 = {"id": "C0", "control": "M0", "rationale": "control"}
V1 = {"id": "V1", "control": "M0", "rationale": "Dave on #town", "mutations": [
    {"axis": "topology", "op": op, "args": {"agent": "dave", "channel": "town"}}
    for op in ("add_reader", "add_writer")]}
WHY = "a test launch"
END = [("request_review", {"conclusions": "Stopped.", "claims": "[]"}),
       ("end_audit", {"conclusions": "Stopped.", "claims": "[]", "response": "Kept."})]
FREE = {"mockllm/l2": (0.0, 0.0)}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def subprocesses(before=None, after=None):
    """Epochs in real subprocesses (no eval may start inside L2's own), with ``before``
    and ``after`` called with the epoch's index (0, 1, ...)."""
    def launch(*args, **kwargs):
        n = launch.calls
        launch.calls += 1
        if before is not None:
            before(n)
        paths = m1.launch_subprocess(*args, **kwargs)
        if after is not None:
            after(n)
        return paths
    launch.calls = 0
    return launch


def ctrl_c() -> None:
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(0.5)  # two signals sent at once would be handled as one


def ctrl_c_once_running() -> None:
    """Ctrl-C as soon as an epoch subprocess is running (from another thread)."""
    def wait_then_press():
        until = time.monotonic() + 60
        while not m1.RUNNING and time.monotonic() < until:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)
    threading.Thread(target=wait_then_press, daemon=True).start()


def audit_with(tmp_path, launch=None, **options):
    clock = Clock()
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True,
                         launch=launch or InProcess(), clock=clock, **options)
    return audit, clock


# ------------------------------------------------------------------- the brief
def test_the_brief_can_set_a_time_limit(tmp_path):
    assert parse_brief(brief(tmp_path, 1.0, extra="\ntime_limit: 5400")).time_limit == 5400
    assert parse_brief(brief(tmp_path, 1.0)).time_limit is None
    for bad in ("0", "-5", "true", "soon"):
        with pytest.raises(BriefError, match="time_limit must be a positive number"):
            parse_brief(brief(tmp_path, 1.0, extra=f"\ntime_limit: {bad}"))


# ------------------------------------------------------------------- the time limit
def test_the_time_limit_stops_new_runs_with_time_held_back(tmp_path):
    audit, clock = audit_with(tmp_path)
    audit.start_clock(1000)
    tools = Tools(audit)
    b = audit.budget()
    assert (b["time_limit_s"], b["time_left_s"], b["time_reserve_s"]) == (1000, 1000,
                                                                          TIME_RESERVE * 1000)
    assert b["halted"] is None and b["stopped"] is None
    ok, text = tools.call("budget")
    assert ok and "Time: 16.7 min left of 16.7 min (1.7 min held back for your report)" in text
    clock.now += 1000 * (1 - TIME_RESERVE)  # only the held-back time is left
    assert "the time limit" in audit.halted and audit.stopped is None
    ok, text = tools.call("run_experiment", variants=[C0], epochs=1, rationale=WHY)
    assert not ok and "the time limit" in text and "no new experiment starts" in text
    ok, text = tools.call("list_runs")  # the query tools still work, and say to conclude
    assert ok and "The audit is stopping: the time limit" in text and "conclude" in text
    ok, text = tools.call("budget")
    assert ok and "STOPPING: the time limit" in text
    assert tools.call("end_audit", conclusions="Out of time.")[0]
    assert audit.write_ledger()["time_left_s"] == pytest.approx(TIME_RESERVE * 1000)


def test_the_time_limit_stops_a_running_experiment_before_its_next_epoch(tmp_path):
    clock = Clock()

    class Slow(InProcess):
        def __call__(self, *args, **kwargs):
            paths = super().__call__(*args, **kwargs)
            clock.now += 600  # each epoch takes 10 minutes
            return paths

    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True, launch=Slow(),
                         clock=clock)
    audit.start_clock(1000)
    tools = Tools(audit)
    ok, text = tools.call("run_experiment", variants=[C0, V1], epochs=3, rationale=WHY)
    assert ok, text
    rows = lines(audit.experiments()[0] / "runs.jsonl")
    assert [r["epochs_done"] for r in rows] == [2, 2]  # 1200 s spent > 900 s; balanced
    decisions = json.loads((audit.experiments()[0] / "decisions.json").read_text())
    assert decisions["stopped"].startswith("before epoch 3: the time limit")
    assert "The audit is stopping" in text and "conclude" in text  # L2 is told


# --------------------------------------------------------------------- by hand
def test_one_interrupt_lets_the_running_experiment_finish_then_l2_concludes(tmp_path):
    audit, _ = audit_with(tmp_path)

    class Interrupted(InProcess):
        def __call__(self, *args, **kwargs):
            if not self.calls:
                audit.interrupt()  # Ctrl-C during the first epoch
            return super().__call__(*args, **kwargs)

    audit.launch_epoch = Interrupted()
    tools = Tools(audit)
    ok, text = tools.call("run_experiment", variants=[C0, V1], epochs=2, rationale=WHY)
    assert ok and [r["epochs_done"] for r in lines(audit.experiments()[0] / "runs.jsonl")] \
        == [2, 2]  # the experiment finished
    assert "The audit is stopping: interrupted by hand" in text
    ok, text = tools.call("run_experiment", variants=[{**V1, "id": "V2"}], epochs=1,
                          rationale=WHY)
    assert not ok and "interrupted by hand" in text
    assert tools.call("scores", variant_ids=["V1"], against="C0", scorer="probe")[0]
    assert tools.call("end_audit", conclusions="Stopped.")[0]


def test_the_first_ctrl_c_interrupts_and_the_second_stops_now(tmp_path, capsys):
    audit, _ = audit_with(tmp_path)
    handle = interrupt_handler(Tools(audit))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             start_new_session=True)
    m1.RUNNING.add(child)
    try:
        handle(signal.SIGINT, None)
        assert audit.interrupted and "Press Ctrl-C again" in capsys.readouterr().err
        assert child.poll() is None and audit.epoch_stop() is None  # it may finish
        with pytest.raises(KeyboardInterrupt):
            handle(signal.SIGINT, None)
        assert child.wait(timeout=10) is not None  # the epoch was stopped too
        assert audit.epoch_stop() == "stopped by hand (Ctrl-C twice)"  # and no next one
    finally:
        m1.RUNNING.discard(child)
        if child.poll() is None:
            child.kill()


def run_stopped(tmp_path, launch, epochs):
    audit, _ = audit_with(tmp_path, launch=launch)
    tools = Tools(audit, on_end=write_report)
    script = [("run_experiment", {"variants": json.dumps([C0]), "epochs": epochs,
                                  "rationale": WHY}), *END]
    before = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        run_l2(tools, fake_model(script), PROMPT, prices=FREE)
    assert signal.getsignal(signal.SIGINT) is before  # put back
    assert not audit.launching and not m1.RUNNING
    report = (audit.out / "report.md").read_text()
    assert "not ended by L2: stopped by hand (Ctrl-C twice)" in report
    ledger = json.loads((audit.out / "ledger.json").read_text())
    assert ledger["final"]
    # the launch's record was written before the audit's
    assert (audit.out / "launches.jsonl").stat().st_mtime <= \
        (audit.out / "report.md").stat().st_mtime
    return audit


def test_a_second_ctrl_c_during_an_epoch_stops_it_and_the_record_is_written(tmp_path):
    def press_during_the_epoch(n):
        ctrl_c()  # the first: the experiment would finish
        ctrl_c_once_running()  # the second, while the epoch runs

    audit = run_stopped(tmp_path, subprocesses(before=press_during_the_epoch), epochs=2)
    [launch] = lines(audit.out / "launches.jsonl")
    assert launch["status"] == "failed" and "stopped by signal" in launch["reason"]
    assert [r["epochs_done"] for r in lines(audit.experiments()[0] / "runs.jsonl")] == [0]


def test_a_second_ctrl_c_between_epochs_starts_no_next_epoch(tmp_path):
    launch = subprocesses(after=lambda n: n == 0 and (ctrl_c(), ctrl_c()))
    audit = run_stopped(tmp_path, launch, epochs=3)
    assert launch.calls == 1
    [row] = lines(audit.out / "launches.jsonl")
    assert row["status"] == "ran" and row["stopped"].startswith("before epoch 2: stopped by "
                                                                "hand")
    assert [r["epochs_done"] for r in lines(audit.experiments()[0] / "runs.jsonl")] == [1]


def test_an_epoch_subprocess_never_outlives_its_launcher(tmp_path, monkeypatch):
    """E.g. Ctrl-C in the fixed-grid CLI: the epoch, in a session of its own, does not get
    the terminal's signal, so the launcher stops it."""
    started = []

    def interrupted(self, *args, **kwargs):
        started.append(self)
        raise KeyboardInterrupt
    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        m1.launch_subprocess(brief(tmp_path, 1.0), ["C0"], 1, tmp_path / "out", True)
    [process] = started
    assert process.wait(timeout=10) is not None and not m1.RUNNING


def test_an_interrupted_fake_audit_reads_its_runs_and_concludes(tmp_path):
    """One SIGINT during the experiment: it finishes, L2 can still read it, and the fake
    goes on to the review and the end."""
    audit, _ = audit_with(tmp_path, launch=subprocesses(lambda n: n == 0 and ctrl_c()))
    tools = Tools(audit, on_end=write_report)
    script = [("run_experiment", {"variants": json.dumps([C0, V1]), "epochs": 2,
                                  "rationale": WHY}),
              ("scores", {"variant_ids": ["V1"], "against": "C0", "scorer": "probe"}), *END]
    run_l2(tools, fake_model(script), PROMPT, prices=FREE)
    trace = [r for r in tools.trace() if r.get("by") != "reviewer"]
    assert [(r["tool"], r["ok"]) for r in trace] == [
        ("run_experiment", True), ("scores", True), ("request_review", True),
        ("end_audit", True)]
    assert [r["epochs_done"] for r in lines(audit.experiments()[0] / "runs.jsonl")] == [2, 2]
    assert "- Stopped early: interrupted by hand" in (audit.out / "report.md").read_text()


# ----------------------------------------------------------------- the agent's clock
def test_l2_is_shown_its_time_limit_and_concludes_even_past_it(tmp_path):
    """Past the whole time limit (e.g. an epoch ran long), L2 still concludes with a
    report: the time limit stops runs, never L2's report."""
    clock = Clock()

    def long(n):
        clock.now += 2000  # longer than the whole limit

    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True,
                         launch=subprocesses(long), clock=clock)
    tools = Tools(audit, on_end=write_report)
    script = [("run_experiment", {"variants": json.dumps([C0]), "epochs": 2,
                                  "rationale": WHY}), *END]
    log = run_l2(tools, fake_model(script), PROMPT, prices=FREE, time_limit=1000)
    sample = resolve_sample_attachments(read_eval_log(log.location).samples[0])
    first = next(e for e in sample.events if e.event == "model")
    user = "\n".join(m.text for m in first.input if m.role == "user")
    assert "- time limit: 16.7 minutes for the whole audit" in user
    assert [r["epochs_done"] for r in lines(audit.experiments()[0] / "runs.jsonl")] == [1]
    assert tools.conclusions == "Stopped."
    assert "- Stopped early: the time limit" in (audit.out / "report.md").read_text()


def test_the_brief_s_time_limit_is_used_and_the_command_line_overrides_it(tmp_path):
    path = brief(tmp_path, 1.0, extra="\ntime_limit: 5400")
    audit = Audit.create(path, tmp_path / "a", mock=True, clock=Clock())
    run_l2(Tools(audit, on_end=write_report), fake_model(END), PROMPT, prices=FREE)
    assert audit.time_limit == 5400
    audit = Audit.create(path, tmp_path / "b", mock=True, clock=Clock())
    run_l2(Tools(audit, on_end=write_report), fake_model(END), PROMPT, prices=FREE,
           time_limit=60)
    assert audit.time_limit == 60


def test_the_cli_exits_130_with_a_record_when_stopped_by_hand(tmp_path, monkeypatch, capsys):
    import experiments.l2.agent as agent

    def stopped(tools, *args, **kwargs):
        write_report(tools, "stopped by hand (Ctrl-C twice)")
        raise KeyboardInterrupt
    monkeypatch.setattr(agent, "run_l2", stopped)
    with pytest.raises(SystemExit) as stop:
        main(["--brief", str(brief(tmp_path, 1.0)), "--out", str(tmp_path / "a"), "--mock",
              "--fake"])
    assert stop.value.code == 130
    assert "report:" in capsys.readouterr().out and (tmp_path / "a" / "report.md").exists()
    with pytest.raises(SystemExit, match="2"):  # argparse's error exit
        main(["--brief", str(brief(tmp_path, 1.0)), "--out", str(tmp_path / "b"), "--mock",
              "--fake", "--time-limit", "0"])
    assert not (tmp_path / "b").exists()
