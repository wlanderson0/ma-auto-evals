"""``COSMOS_STREAM=1``: ``experiments.run`` opens one cosmos live run for all its processes.

The ``cosmos`` CLI is a stand-in script on ``PATH`` that records how it was called;
nothing here imports cosmos or touches the network.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

import experiments.run as run
from experiments.run import LIVE_VARIABLES, main

SLUG = "r3a9k2xq"
KEY = "cosk_Zx81QpLm_0123456789abcdefghijABCDEFGHIJ-_"
FAKE_COSMOS = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["FAKE_COSMOS_CALLS"], "a") as calls:
    calls.write(json.dumps({{"argv": sys.argv[1:],
                            "joined": os.environ.get("COSMOS_LIVE_RUN")}}) + "\\n")
if sys.argv[1:3] == ["live", "open"]:
    code = int(os.environ.get("FAKE_COSMOS_EXIT", "0"))
    if code:
        print("could not reach cosmos", file=sys.stderr)
        sys.exit(code)
    print("export COSMOS_LIVE_RUN={SLUG}")
    print("export COSMOS_RUN_KEY={KEY}")
"""
ONE = ["--mock", "--scenario", "mfa_rumor", "--conditions", "baseline", "--epochs", "1"]


@pytest.fixture
def cosmos(tmp_path, monkeypatch):
    """A stand-in ``cosmos`` on PATH; returns a function that lists the calls made to it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "cosmos"
    script.write_text(FAKE_COSMOS)
    script.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_COSMOS_CALLS", str(calls))
    for name in ("COSMOS_STREAM", *LIVE_VARIABLES):
        monkeypatch.delenv(name, raising=False)
    return lambda: ([json.loads(line) for line in calls.read_text().splitlines()]
                    if calls.exists() else [])


def test_streaming_is_off_unless_asked_for(tmp_path, cosmos):
    out = tmp_path / "run"
    main([*ONE, "--out", str(out)])
    assert cosmos() == []
    assert "live_run" not in json.loads((out / "manifest.json").read_text())


def test_one_live_run_is_opened_recorded_and_ended(tmp_path, cosmos, monkeypatch, capsys):
    monkeypatch.setenv("COSMOS_STREAM", "1")
    out = tmp_path / "run"
    main([*ONE, "--out", str(out)])
    assert [call["argv"] for call in cosmos()] == [
        ["live", "open", "--manifest", str(out / "manifest.json"), "--samples-total", "1"],
        ["live", "end", "--state", "finished"]]
    # The end call still carries the run it ends; afterwards nothing is left behind.
    assert cosmos()[1]["joined"] == SLUG
    assert not any(name in os.environ for name in LIVE_VARIABLES)
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["live_run"] == SLUG
    assert manifest["finished_at"] is not None
    printed = capsys.readouterr()
    assert f"cosmos: live run {SLUG}" in printed.out
    assert KEY not in printed.out + printed.err
    assert KEY not in (out / "manifest.json").read_text()


def test_every_condition_process_joins_the_same_run(tmp_path, cosmos, monkeypatch):
    monkeypatch.setenv("COSMOS_STREAM", "1")
    seen = []

    def child(name, condition, args, out):
        seen.append({name: os.environ.get(name) for name in LIVE_VARIABLES})
        return []

    monkeypatch.setattr(run, "_run_in_subprocess", child)
    main(["--mock", "--scenario", "mfa_rumor", "--conditions", "baseline,l1", "--epochs", "2",
          "--out", str(tmp_path / "run")])
    assert seen == [{"COSMOS_LIVE_RUN": SLUG, "COSMOS_RUN_KEY": KEY,
                     "COSMOS_LIVE_ROOT": str((tmp_path / "run").resolve())}] * 2
    opened = [call["argv"] for call in cosmos() if call["argv"][:2] == ["live", "open"]]
    assert len(opened) == 1 and opened[0][-2:] == ["--samples-total", "4"]


def test_a_child_process_never_opens_a_run_of_its_own(tmp_path, cosmos, monkeypatch):
    monkeypatch.setenv("COSMOS_STREAM", "1")
    main([*ONE, "--out", str(tmp_path), "--no-manifest", "--results-name", "part.json"])
    assert cosmos() == []


def test_a_run_a_wrapper_opened_is_joined_and_left_open(tmp_path, cosmos, monkeypatch):
    monkeypatch.setenv("COSMOS_STREAM", "1")
    monkeypatch.setenv("COSMOS_LIVE_RUN", "wrapper1")
    monkeypatch.setenv("COSMOS_RUN_KEY", "cosk_wrapper")
    main([*ONE, "--out", str(tmp_path / "run")])
    assert cosmos() == []
    assert os.environ["COSMOS_LIVE_RUN"] == "wrapper1"


@pytest.mark.parametrize("broken", ["exit", "missing", "garbled"])
def test_the_run_goes_on_when_cosmos_cannot_open_one(tmp_path, cosmos, monkeypatch, capsys,
                                                     broken):
    monkeypatch.setenv("COSMOS_STREAM", "1")
    if broken == "exit":
        monkeypatch.setenv("FAKE_COSMOS_EXIT", "1")
    elif broken == "missing":
        (tmp_path / "bin" / "cosmos").unlink()
    else:
        (tmp_path / "bin" / "cosmos").write_text(f"#!{sys.executable}\nprint('export X=1')\n")
    out = tmp_path / "run"
    main([*ONE, "--out", str(out)])
    assert "no live run opened" in capsys.readouterr().out
    assert "live_run" not in json.loads((out / "manifest.json").read_text())
    assert (out / "results.json").exists()
    assert [call["argv"][:2] for call in cosmos()] in ([], [["live", "open"]])
    assert not any(name in os.environ for name in LIVE_VARIABLES)


@pytest.mark.parametrize("error, state", [(RuntimeError("boom"), "error"),
                                          (KeyboardInterrupt(), "cancelled")])
def test_a_run_that_stops_early_says_how(tmp_path, cosmos, monkeypatch, error, state):
    monkeypatch.setenv("COSMOS_STREAM", "1")

    def stop(*args):
        raise error

    monkeypatch.setattr(run, "_run_all", stop)
    with pytest.raises(type(error)):
        main([*ONE, "--out", str(tmp_path / "run")])
    assert cosmos()[-1]["argv"] == ["live", "end", "--state", state]
    assert not any(name in os.environ for name in LIVE_VARIABLES)
