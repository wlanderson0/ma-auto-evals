"""The L2 audit's progress views (``experiments.l2.progress``)."""

from __future__ import annotations

import io
from types import SimpleNamespace

from rich.console import Console

from experiments.l2.progress import Lines, Status, duration, reporter


def budget(spent: float = 0.25, stopped: str | None = None) -> dict:
    return {"spent_usd": spent, "budget_usd": 1.0, "stopped": stopped,
            "by_kind": {"l2": 0.05, "experiments": spent - 0.05}}


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def events() -> list[dict]:
    b = budget()
    return [
        {"event": "tool", "name": "run_experiment", "done": False, "budget": b},
        {"event": "launch", "experiment": "exp-1", "variants": ["a", "b"], "epochs": 2,
         "estimate_usd": 0.4, "available_usd": 0.85, "budget": b},
        {"event": "epoch", "experiment": "exp-1", "epoch": 1, "epochs": 2, "done": False,
         "budget": b},
    ]


def test_duration():
    assert [duration(s) for s in (5, 65, 3725)] == ["5s", "1m05s", "1h02m05s"]


def test_the_panel_shows_what_runs_now_the_spend_and_the_recent_events():
    clock = Clock()
    status = Status("openrouter/x/y", clock=clock)
    for event in events():
        clock.t += 30
        status.update(event)
    clock.t += 95
    console = Console(file=io.StringIO(), width=120, record=True)
    console.print(status.render())
    text = console.export_text()
    assert "L2 audit · openrouter/x/y" in text
    assert "exp-1 epoch 1/2 is running (1m35s)" in text
    assert "Elapsed  3m05s" in text
    assert "$0.2500 of $1.00 (L2 $0.0500, runs $0.2000)" in text
    assert "1 experiment(s), 0 epoch(s), 0 tool call(s)" in text
    assert "+1m00s  exp-1: 2 variant(s) x 2 epoch(s), estimated $0.4000 of $0.8500" in text
    assert "Stopped" not in text

    status.update({"event": "epoch", "experiment": "exp-1", "epoch": 1, "epochs": 2,
                   "done": True, "seconds": 95, "budget": budget(0.9, "the hard stop")})
    status.update({"event": "tool", "name": "run_experiment", "done": True, "ok": True,
                   "first_line": "Ran exp-1.", "budget": budget(0.9, "the hard stop")})
    console = Console(file=io.StringIO(), width=120, record=True)
    console.print(status.render())
    text = console.export_text()
    assert "Now      L2 is thinking (0s)" in text
    assert "1 experiment(s), 1 epoch(s), 1 tool call(s)" in text
    assert "exp-1 epoch 1/2: done in 1m35s" in text and "L2 called run_experiment: Ran" in text
    assert "Stopped  the hard stop" in text


def test_lines_print_one_line_per_event_with_the_spend():
    out = io.StringIO()
    lines = Lines(Status("m"), Console(file=out, width=80))
    for event in events():
        lines(event)
    lines({"event": "spend", "budget": budget()})  # moves the state only
    lines({"event": "tool", "name": "scores", "done": True, "ok": False,
           "first_line": "unknown variant(s) ['x']", "budget": budget()})
    printed = out.getvalue().splitlines()
    assert len(printed) == 3  # a tool starting and a spend event print nothing
    assert printed[1].endswith("exp-1 epoch 1/2: running; spent $0.2500 of $1.00 "
                               "(L2 $0.0500, runs $0.2000)")
    assert "L2 called scores (refused): unknown variant(s) ['x']; spent" in printed[2]


def test_the_reporter_picks_the_view_and_unhooks_it():
    audit = SimpleNamespace(progress=None)
    with reporter(audit, quiet=True) as status:
        assert status is None and audit.progress is None
    with reporter(audit, console=Console(file=io.StringIO())):
        assert isinstance(audit.progress, Lines)
    assert audit.progress is None
    terminal = io.StringIO()
    with reporter(audit, model="m", console=Console(file=terminal, force_terminal=True,
                                                    width=100)) as status:
        assert audit.progress == status.update
        audit.progress(events()[1])
    assert audit.progress is None
    assert "L2 audit · m" in terminal.getvalue()
