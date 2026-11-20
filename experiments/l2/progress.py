"""What an L2 audit is doing while it runs (``python -m experiments.l2.audit``).

The audit and its tools report progress events (``Audit.say``): a tool call starting
or ending, an experiment launching, an epoch starting or ending, L2's own spend moving.
On a terminal the CLI shows them as a live status panel that redraws in place (what
is running now and for how long, the spend against the budget, counts, the last few
events); otherwise (output piped or logged) as one timestamped line per event.
Nothing here is recorded: the audit directory is the record.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

RECENT = 6
"""Events the panel lists under "Recent"."""


def duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def spend(b: dict[str, Any]) -> str:
    return (f"${b['spent_usd']:.4f} of ${b['budget_usd']:.2f} "
            f"(L2 ${b['by_kind']['l2']:.4f}, runs ${b['by_kind']['experiments']:.4f})")


def _short(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Status:
    """The audit's progress so far, from its events; both views read it."""

    def __init__(self, model: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.model = model
        self.clock = clock
        self.start = self.since = clock()
        self.now = "starting"
        self.budget: dict[str, Any] | None = None
        self.tool_calls = self.experiments = self.epochs = 0
        self.recent: deque[str] = deque(maxlen=RECENT)
        self.live: Any = None
        """The live panel while it is shown (``reporter``), so it can pause."""

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Stop redrawing the live panel while the block runs (e.g. while the user
        answers a question in the terminal), then show it again."""
        live = self.live
        if live is None:
            yield
            return
        live.stop()
        try:
            yield
        finally:
            live.start()

    def _doing(self, now: str) -> None:
        self.now, self.since = now, self.clock()

    def update(self, event: dict[str, Any]) -> str | None:
        """Take one event; return its line (None for one that only moves the state)."""
        self.budget = event.get("budget", self.budget)
        kind, line = event["event"], None
        if kind == "tool" and not event["done"]:
            self._doing(f"L2 is calling {event['name']}")
        elif kind == "tool":
            self.tool_calls += 1
            line = f"L2 called {event['name']}"
            if not event["ok"]:
                line += f" (refused): {_short(event['first_line'])}"
            elif event["name"] in ("run_experiment", "end_audit"):
                line += f": {_short(event['first_line'])}"
            ended = event["name"] == "end_audit" and event["ok"]
            self._doing("L2 ended the audit; writing the report" if ended else "L2 is thinking")
        elif kind == "launch":
            self.experiments += 1
            line = (f"{event['experiment']}: {len(event['variants'])} variant(s) x "
                    f"{event['epochs']} epoch(s), estimated ${event['estimate_usd']:.4f} of "
                    f"${event['available_usd']:.4f} available")
        elif kind == "epoch" and not event["done"]:
            self._doing(f"{event['experiment']} epoch {event['epoch']}/{event['epochs']} "
                        "is running")
            line = f"{event['experiment']} epoch {event['epoch']}/{event['epochs']}: running"
        elif kind == "epoch":
            self.epochs += 1
            line = (f"{event['experiment']} epoch {event['epoch']}/{event['epochs']}: done in "
                    f"{duration(event['seconds'])}")
        if line is not None:
            self.recent.append(f"+{duration(self.clock() - self.start)}  {line}")
        return line

    def render(self) -> Panel:
        now = self.clock()
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold", no_wrap=True)
        grid.add_column()
        grid.add_row("Now", f"{self.now} ({duration(now - self.since)})")
        grid.add_row("Elapsed", duration(now - self.start))
        if self.budget is not None:
            b = self.budget
            bar = ProgressBar(total=max(b["budget_usd"], 1e-9),
                              completed=min(b["spent_usd"], b["budget_usd"]), width=30)
            row = Table.grid(padding=(0, 1))
            row.add_row(bar, Text(spend(b)))
            grid.add_row("Spend", row)
            if b["stopped"]:
                grid.add_row("Stopped", Text(b["stopped"], style="bold red"))
        grid.add_row("Done", f"{self.experiments} experiment(s), {self.epochs} epoch(s), "
                             f"{self.tool_calls} tool call(s)")
        grid.add_row("Recent", Text("\n".join(self.recent) or "-", style="dim"))
        return Panel(grid, title=f"L2 audit · {self.model}", title_align="left")


class Lines:
    """One timestamped line per event, with the spend so far (output not a terminal)."""

    def __init__(self, status: Status, console: Console) -> None:
        self.status, self.console = status, console

    def __call__(self, event: dict[str, Any]) -> None:
        line = self.status.update(event)
        if line is not None:
            elapsed = duration(self.status.clock() - self.status.start)
            self.console.print(f"[{time.strftime('%H:%M:%S')} +{elapsed}] {line}; "
                               f"spent {spend(event['budget'])}", markup=False, highlight=False,
                               soft_wrap=True)


@contextmanager
def reporter(audit: Any, *, quiet: bool = False, model: str = "L2",
             console: Console | None = None) -> Iterator[Status | None]:
    """Show ``audit``'s progress while the block runs: the live panel on a terminal,
    lines otherwise, nothing when ``quiet``."""
    if quiet:
        yield None
        return
    console = console or Console()
    status = Status(model)
    if not console.is_terminal:
        audit.progress = Lines(status, console)
        try:
            yield status
        finally:
            audit.progress = None
        return
    from rich.live import Live

    audit.progress = status.update
    try:
        with Live(get_renderable=status.render, console=console,
                  refresh_per_second=2) as live:
            status.live = live
            yield status
    finally:
        status.live = None
        audit.progress = None
