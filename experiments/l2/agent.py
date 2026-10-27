"""Run L2 as an Inspect agent over an audit (plan ``docs/level2-milestone2-mvp.md``,
steps 4 and 5).

L2 is Inspect's ``react`` agent with the tools of ``experiments.l2.tools``, in an
Inspect task of its own whose log goes to ``<audit>/l2/``; ``end_audit`` is its
submit tool, so the loop ends when L2 ends the audit (a refused ``end_audit``
does not). Experiments run in subprocesses (milestone 1's ``launch_subprocess``),
so no eval starts inside this one.

L2's own spend is read from its model events before every tool call and after
every turn. Once it has spent the whole budget, the loop stops; the hard stop
(spent plus reserve) is applied by the tools. A token limit derived from the
budget is the backstop. When the task ends, ``finalize`` prices L2's calls from
its saved log, writes the final ledger and (re)writes ``report.md``, so the record
is complete however L2 stopped.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from inspect_ai import Task
from inspect_ai import eval as inspect_eval
from inspect_ai.agent import AgentState, AgentSubmit, react
from inspect_ai.dataset import Sample
from inspect_ai.log import EvalLog, read_eval_log
from inspect_ai.model import Model
from inspect_ai.tool import ToolDef

from experiments.l2.audit import EPSILON, TEXT_OPS
from experiments.l2.audit_report import write_report
from experiments.l2.cost import PRICES, UnknownPrice, log_cost, price
from experiments.l2.tools import Tools, describe, inspect_tools, transcript_spend

MESSAGE_LIMIT = 300
"""A backstop on L2's conversation length, whatever the model costs."""
CONTINUE = ("Keep going with the tools. When you are done, or the budget is nearly spent, "
            "call end_audit with your conclusions.")


def token_backstop(budget_usd: float, model: str,
                   prices: Mapping[str, tuple[float, float]] = PRICES) -> int | None:
    """Tokens that cost the whole budget at the model's dearer rate (``None`` if free)."""
    dearest = max(price(model, prices))
    return None if dearest <= 0 else int(budget_usd * 1_000_000 / dearest)


def brief_message(tools: Tools) -> str:
    """The first user message: the brief, the controls and the changes L2 can make."""
    audit = tools.audit
    brief = audit.brief
    lines = ["# The brief", "", brief.question.strip(), ""]
    for name in ("mode", "measure", "base_scenarios", "allowed_changes", "hypotheses"):
        if (value := getattr(brief, name)) is not None:
            value = (", ".join(value) if isinstance(value, tuple)
                     else " ".join(str(value).split()))
            lines.append(f"- {name} (fixed by the brief): {value}")
    lines.append(f"- budget: ${brief.budget_usd:.2f} for everything, your own calls "
                 f"included; {audit.reserve:.0%} is held back for your final calls")
    if audit.analysis is not None:
        lines += ["", "This audit is analysis only: you cannot run experiments. Read the "
                  "runs already made with the query tools."]
    elif brief.controls:
        lines += ["", "# Controls", "",
                  "Each variant you run is a copy of one of these with changes added:"]
        for control in brief.controls:
            mutations = "; ".join(describe(_plain(m)) for m in control.mutations) or "none"
            lines += [f"- {control.id} (base {control.base}, inner "
                      f"{control.inner.condition}): {control.rationale}",
                      f"  its mutations: {mutations}"]
        lines += ["", "# Changes you can add", "",
                  "- topology: add_reader / remove_reader / add_writer / remove_writer, "
                  "args {agent, channel}",
                  "- schedule: set_rounds, args {rounds}; activate_agent, args {agent, "
                  "position: \"after:<agent>\"} with at: \"after_round:<n>\" (an agent in "
                  "the setup with no turns, e.g. the Advocate in M6)",
                  f"- not allowed: {', '.join(sorted(TEXT_OPS))} (setup texts come from the "
                  "controls)"]
        if brief.allowed_changes is not None:
            lines.append(f"- the brief allows only these axes: "
                         f"{', '.join(brief.allowed_changes)}")
    return "\n".join(lines)


def _plain(mutation: Any) -> dict[str, Any]:
    return {"axis": mutation.axis, "op": mutation.op, "args": mutation.args, "at": mutation.at}


def l2_task(tools: Tools, prompt: str, *, prices: Mapping[str, tuple[float, float]] = PRICES,
            token_limit: int | None = None, message_limit: int | None = None) -> Task:
    audit = tools.audit

    def spend() -> dict[str, Any]:
        return transcript_spend(prices)

    made = inspect_tools(tools, spend=spend, experiments=audit.analysis is None)
    end = next(t for t in made if ToolDef(t).name == "end_audit")
    others = [t for t in made if t is not end]

    async def on_continue(state: AgentState) -> bool | str:
        try:
            audit.set_l2_spend(spend())
        except Exception as error:  # e.g. no price for L2's model: stop rather than run blind
            tools.stop_reason = f"L2's own spend could not be read: {error}"
            return False
        if audit.ended:
            return False
        if audit.budget()["remaining_usd"] <= EPSILON:
            tools.stop_reason = "L2 spent the whole budget before calling end_audit"
            return False
        return True if state.output.message.tool_calls else CONTINUE

    agent = react(name="l2", prompt=prompt, tools=others,
                  submit=AgentSubmit(tool=end, answer_only=True, keep_in_messages=True),
                  on_continue=on_continue)
    return Task(dataset=[Sample(input=brief_message(tools), id="l2")], solver=agent,
                token_limit=token_limit, message_limit=message_limit or MESSAGE_LIMIT,
                metadata={"l2_audit": str(audit.out)}, name="l2-audit")


def run_l2(tools: Tools, model: str | Model, prompt: str, *,
           prices: Mapping[str, tuple[float, float]] = PRICES,
           token_limit: int | None = None) -> EvalLog:
    """Run L2 over ``tools.audit`` to the end, then ``finalize``. Without a
    ``token_limit``, the backstop is the budget's worth of the model's tokens."""
    if token_limit is None:
        try:
            token_limit = token_backstop(tools.audit.brief.budget_usd, str(model), prices)
        except UnknownPrice:
            token_limit = None  # the ledger then stops L2 at its first tool call
    task = l2_task(tools, prompt, prices=prices, token_limit=token_limit)
    tools.stop_reason = None
    [log] = inspect_eval(task, model=model, log_dir=str(tools.audit.out / "l2"),
                         display="none", fail_on_error=False)
    finalize(tools, log, prices)
    return log


def finalize(tools: Tools, log: EvalLog, prices: Mapping[str, tuple[float, float]] = PRICES
             ) -> None:
    """Price L2's calls from its saved log, write the final ledger and the report. Never
    raises on a bad log: the record is written whatever happened."""
    audit = tools.audit
    notes = []
    try:
        saved = read_eval_log(log.location) if log.location else log
    except Exception as error:
        saved, notes = log, [f"L2's saved log could not be read ({error})"]
    try:
        cost = log_cost(saved, prices)
        if cost["usd"] < audit.l2["usd"] - EPSILON:
            # e.g. a cancelled run, whose log lost the sample that was still running
            notes.append(f"L2's log prices its calls at ${cost['usd']:.6f}, less than the "
                         f"${audit.l2['usd']:.6f} seen during the run; the higher is kept")
        else:
            audit.set_l2_spend({"usd": cost["usd"], "by_model": cost["by_model"]})
    except Exception as error:  # e.g. no price for L2's model
        notes.append(f"L2's calls could not be priced from its log ({error}); the ledger "
                     "keeps the figure seen during the run")
    why = None
    if tools.conclusions is None:
        sample = (saved.samples or [None])[0]
        if tools.stop_reason:
            why = tools.stop_reason
        elif sample is not None and sample.limit is not None:
            why = f"the {sample.limit.type} limit ({sample.limit.limit:g}) stopped L2"
        elif sample is not None and sample.error is not None:
            why = f"L2's run errored: {sample.error.message[:500]}"
        elif saved.status != "success":
            why = f"L2's run ended with status {saved.status}"
        else:
            why = "L2 stopped without calling end_audit"
    audit.notes.extend(notes)
    audit.ended = True
    audit.write_ledger(final=True)
    write_report(tools, why)
