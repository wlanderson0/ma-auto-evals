"""Asking the user to approve L2's dosage plan (``plan_dosage``) before any experiment.

Opt-in, so an unattended audit never waits: with ``--plan-approval`` the CLI
(``python -m experiments.l2.audit``) sends an LLM L2's plan to the person running the
audit, in the terminal: the live panel pauses, the plan is shown, and they answer yes or
no with an optional note for L2. L2 gets the answer and the note as the tool's result; no
experiment runs until a plan is approved. Once one is, revisions hold at once, unless
``--approve-revisions`` sends each of them too. The answer is recorded with the plan
(``dosage-plan.jsonl``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

Approver = Callable[[Any, str], dict[str, Any]]
"""``approve(plan, text) -> {"approved": bool, "comment": str}``."""


def terminal_approver(status_of: Callable[[], Any] = lambda: None,
                      console: Any = None) -> Approver:
    """An approver that asks in the terminal. ``status_of()`` gives the progress panel's
    ``Status`` (or ``None``), so the live panel pauses while the user answers."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    console = console or Console()

    def approve(plan: Any, text: str) -> dict[str, Any]:
        status = status_of()
        paused = status.paused() if status is not None else nullcontext()
        with paused:
            # Text, so brackets in L2's wording (e.g. "[Kestrel]") are never read as markup
            console.print(Panel(Text(text), title="L2's dosage plan, for your approval",
                                title_align="left"))
            try:
                answer = ""
                while answer not in ("y", "yes", "n", "no"):
                    answer = console.input("Approve this plan? [y]es / [n]o: ",
                                           markup=False).strip().lower()
                comment = console.input("A note for L2 (optional; Enter to skip): ",
                                        markup=False).strip()
            except EOFError:  # no answer can come: never approve by default
                answer, comment = "n", "no answer (the terminal closed)"
        return {"approved": answer.startswith("y"), "comment": comment}

    return approve


def decide(approve: Approver, plan: Any, text: str) -> dict[str, Any]:
    """Ask ``approve``; the answer as recorded. An approver that fails never approves."""
    try:
        answer = approve(plan, text)
        approved, comment = bool(answer.get("approved")), str(answer.get("comment") or "")
    except Exception as error:
        approved, comment = False, f"the approval failed: {type(error).__name__}: {error}"
    return {"approved": approved, "comment": " ".join(comment.split()), "by": "user",
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
