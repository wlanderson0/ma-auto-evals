"""The L2 audit's final report, ``report.md`` (plan step 3; spec §8, §6.3).

It holds L2's conclusions from ``end_audit`` (quoted, or why there are none), every
variant run with what it changed and why, the tables the conclusions rest on
(every distinct comparison and score table L2 asked for, read again from the
logs), the search counts (counted from the record, not taken from L2), where L2's
reading departed from the guidance (checked automatically; L2's own account is in
its conclusions), the decisions L2 recorded, and the ledger. It is written when L2
ends the audit and again when the run finishes (with the final ledger), so it is
always complete. Records it cannot read in part (a line cut by a crash) are noted,
never fatal.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from experiments.l2.report import SCORER
from experiments.l2.tools import Tools, _ids, _jsonl

RECORD = {
    "brief.md": "the brief as given",
    "decisions.jsonl": "L2's choices on what the brief left open, with reasons",
    "proposed.jsonl": "every variant L2 proposed, rejected ones too (with the trace seq)",
    "launches.jsonl": "every launch L2 asked for and what came of it",
    "trace.jsonl": "every tool call and its (shortened) result, in order",
    "ledger.json": "spend by kind, reserve, remaining",
    "experiments/": "one milestone 1 audit directory per launch (eval logs under logs/)",
    "l2/": "L2's own Inspect log",
    "report.md": "this report",
}
RANK = {"run": 2, "rejected": 1, "not run": 0}


def _plain(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def comparisons(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distinct successful ``compare`` calls, first-call order; a and b swapped, or
    ``rounds`` left out, is the same comparison."""
    seen, out = set(), []
    for row in trace:
        if row.get("tool") != "compare" or not row.get("ok"):
            continue
        args = {"a": row["args"]["a"], "b": row["args"]["b"], "scorer": row["args"]["scorer"],
                "rounds": row["args"].get("rounds")}
        key = (frozenset((args["a"], args["b"])), args["scorer"], args["rounds"])
        if key not in seen:
            seen.add(key)
            out.append(args)
    return out


def score_tables(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for row in trace:
        if row.get("tool") != "scores" or not row.get("ok"):
            continue
        ids = _ids(row["args"]["variant_ids"])
        key = (tuple(sorted(ids)), row["args"]["scorer"])
        if key not in seen:
            seen.add(key)
            out.append({"variant_ids": ids, "scorer": row["args"]["scorer"]})
    return out


def proposals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per distinct proposal (the same variant sent again counts once; its
    rationale's wording does not matter), with its best outcome: run > rejected > not
    run (e.g. a variant re-sent after it ran is rejected as a repeat, but it ran)."""
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = row.get("proposed")
        if isinstance(entry, dict):
            key = json.dumps({k: entry.get(k) for k in ("id", "base", "control", "mutations",
                                                        "inner")}, sort_keys=True)
        else:
            key = json.dumps(entry, sort_keys=True, default=str)
        outcome = ("run" if row.get("experiment") and row.get("epochs_done") else
                   "rejected" if row.get("status") == "rejected" or row.get("launch")
                   in ("refused", "rejected") and row.get("status") != "accepted" else
                   "not run")
        if key not in best or RANK[outcome] > RANK[best[key]["outcome"]]:
            best[key] = {**row, "outcome": outcome}
    return list(best.values())


def render(tools: Tools, why_no_conclusions: str | None = None) -> str:
    audit = tools.audit
    brief = audit.brief
    trace = tools.trace()
    out = [f"# L2 audit report: {audit.out.name}", ""]
    status = ("ended by L2 (end_audit)" if tools.conclusions is not None else
              f"not ended by L2: {why_no_conclusions or 'end_audit was never called'}")
    where = (f"Analysis only, on the runs in {audit.analysis}." if audit.analysis else
             "Experiments ran on " + ("the offline mock models." if audit.mock else
                                      "live models."))
    out += [f"Status: {status}. {where}", ""]

    out += ["## Brief", "", *[f"> {line}" for line in brief.question.splitlines()], ""]
    fixed = {name: getattr(brief, name) for name in (
        "mode", "base_scenarios", "base_logs", "measure", "allowed_changes", "variants",
        "controls", "hypotheses", "inner")}
    for name, value in fixed.items():
        if value is None:
            continue
        if name in ("variants", "controls"):
            value = [v.id for v in value]
        elif name == "inner":
            value = value.condition
        out.append(f"- Fixed by the brief: `{name}` = {_plain(value)}")
    open_ = [name for name, value in fixed.items() if value is None]
    out += [f"- Budget: ${brief.budget_usd:.2f}", f"- Left to L2: {', '.join(open_) or 'none'}",
            ""]

    out += ["## Conclusions (L2's own words, quoted)", ""]
    if tools.conclusions is not None:
        out += [f"> {line}" if line.strip() else ">" for line in tools.conclusions.splitlines()]
    else:
        out.append(f"None: {why_no_conclusions or 'L2 never called end_audit'}.")
    out.append("")

    decisions = _jsonl(audit.out / "decisions.jsonl")
    out += ["## Decisions on what the brief left open", ""]
    out += [f"- `{d['field']}` = {d['value']}: {d['reason']}" for d in decisions] or [
        "None recorded."]
    out.append("")

    launches = _jsonl(audit.out / "launches.jsonl")
    launched = [row for row in launches if row.get("experiment")]
    out += ["## Experiments", ""]
    if launched:
        out += ["| experiment | variants | epochs asked | status | stopped | spent |",
                "|---|---|---|---|---|---|"]
        out += [f"| {r['experiment']} | {', '.join(r['ids'])} | {r['epochs']} | {r['status']} "
                f"| {r.get('stopped') or '-'} | ${r.get('spent_usd') or 0:.4f} |"
                for r in launched]
    else:
        out.append("No experiment was launched in this audit.")
    others = Counter(r["status"] for r in launches if not r.get("experiment"))
    if others:
        out.append("Launches that did not run: " + ", ".join(
            f"{n} {status}" for status, n in sorted(others.items())) + " (see launches.jsonl).")
    out.append("")

    try:
        variants = tools.variant_rows()
    except Exception as error:  # the report is still written
        variants, out = [], out + [f"(The variants could not be read: {error})", ""]
    out += ["## Variants (what each changed and why)", ""]
    if variants:
        out += ["| variant | experiment | base | changes | condition | status | "
                "epochs | rationale |", "|---|---|---|---|---|---|---|---|"]
        out += [f"| {v['id']} | {v['experiment']} | {v['base']} | {v['changes']} | "
                f"{v['condition']} | {v['status']} | {v['epochs']} | "
                f"{' '.join(v['rationale'].split())} |" for v in variants]
    else:
        out.append("No variant ran.")
    out.append("")

    compares, scores = comparisons(trace), score_tables(trace)
    out += ["## Tables the conclusions rest on", "",
            "Every distinct comparison and score table L2 asked for, read again from the logs.",
            ""]
    for args in compares:
        rounds = f", up to round {args['rounds']}" if args["rounds"] is not None else ""
        out += [f"### compare {args['a']} vs {args['b']} ({args['scorer']}{rounds})", "",
                _again(tools.compare, args), ""]
    for args in scores:
        out += [f"### scores {', '.join(args['variant_ids'])} ({args['scorer']})", "",
                _again(tools.scores, args), ""]
    if not compares and not scores:
        out += ["L2 asked for no comparison or score table.", ""]

    proposed = proposals(_jsonl(audit.out / "proposed.jsonl"))
    outcomes = Counter(p["outcome"] for p in proposed)
    scorers = sorted({a["scorer"] for a in compares + scores})
    calls = Counter(row.get("tool") for row in trace)
    ok_compares = sum(row.get("tool") == "compare" and bool(row.get("ok")) for row in trace)
    failed = sum(not row.get("ok") for row in trace)
    with_epochs = sum(1 for r in launched if r.get("spent_usd") or any(
        v["epochs"] for v in variants if v["experiment"] == r["experiment"]))
    out += ["## Search counts (from the record)", "",
            f"- Variants proposed: {len(proposed)} ({outcomes['run']} run, "
            f"{outcomes['rejected']} rejected, {outcomes['not run']} not run)",
            f"- Experiments launched: {len(launched)} ({with_epochs} with at least one epoch "
            f"done); launch attempts: {len(launches)}",
            f"- Scorers read: {len(scorers)} ({', '.join(scorers) or 'none'})",
            f"- Comparisons tried: {len(compares)} distinct ({ok_compares} successful calls)",
            f"- Transcripts read: {calls['transcript']}; searches: {calls['find']}",
            f"- Tool calls: {len(trace)} ({failed} refused or failed)", ""]

    out += ["## Departures from the reading guidance (automatic checks)", ""]
    notes = []
    for args in compares:
        try:
            found = tools.departures(args["a"], args["b"], args["scorer"], args["rounds"])
        except Exception as error:
            found = [f"could not check: {type(error).__name__}: {error}"]
        notes += [f"- compare {args['a']} vs {args['b']} ({args['scorer']}): {n}"
                  for n in found]
    for args in scores:
        notes += [f"- scores {', '.join(args['variant_ids'])} ({args['scorer']}): {n}"
                  for n in _score_departures(tools, args["variant_ids"])]
    if brief.measure is not None:
        for args in compares + scores:
            ids = [args["a"], args["b"]] if "a" in args else args["variant_ids"]
            try:
                bases = {tools.results().base(vid) for vid in ids}
            except Exception:
                continue
            expected = {SCORER.get(base) for base in bases}
            if args["scorer"] not in expected:
                notes.append(f"- {args['scorer']} read on {', '.join(ids)}: the brief fixes the "
                             f"measure ({' / '.join(sorted(filter(None, expected)))} here), so "
                             "this may be reported alongside but is not the evidence.")
    out += list(dict.fromkeys(notes)) or ["None found."]
    out += ["", "L2's own account of where it departed from the guidance is in its "
            "conclusions.", ""]

    b = audit.budget()
    out += ["## Ledger", "",
            f"- Budget ${b['budget_usd']:.2f}; spent ${b['spent_usd']:.4f}; remaining "
            f"${b['remaining_usd']:.4f}; reserve ${b['reserve_usd']:.4f}",
            f"- By kind: experiments ${b['by_kind']['experiments']:.4f}, L2's own calls "
            f"${b['by_kind']['l2']:.4f}"]
    out += [f"- {name}: ${usd:.4f}" for name, usd in b["by_experiment"].items()]
    out += [f"- L2 on {model}: ${row.get('usd', 0):.4f} ({row.get('calls', '?')} calls, "
            f"{row.get('input_tokens', '?')} input / {row.get('output_tokens', '?')} output "
            "tokens)" for model, row in b["l2_by_model"].items()]
    if b["stopped"]:
        out.append(f"- Stopped: {b['stopped']}")
    out += [f"- Problem: {note}" for note in audit.notes]
    if b["spent_usd"] > b["budget_usd"]:
        out.append(f"- Over budget by ${b['spent_usd'] - b['budget_usd']:.4f}: a launched "
                   "epoch runs to completion, so spending can exceed the budget by up to "
                   "one epoch.")
    out.append("")

    out += ["## Record", ""]
    out += [f"- `{name}`: {what}" for name, what in RECORD.items()
            if (audit.out / name.rstrip("/")).exists() or name in ("trace.jsonl", "report.md")]
    return "\n".join(out) + "\n"


def _score_departures(tools: Tools, ids: list[str]) -> list[str]:
    """A score table read across variants from different experiments or epochs."""
    try:
        results = tools.results()
        where = {vid: tools._where.get(vid) for vid in ids}
        epochs = {vid: len(results.epochs(vid)) for vid in ids}
    except Exception as error:
        return [f"could not check: {type(error).__name__}: {error}"]
    notes = []
    if len(set(where.values())) > 1:
        notes.append(f"Note: the variants ran in different experiments ({where}); the guidance "
                     "is to run them in the same experiment.")
    if len(set(epochs.values())) > 1:
        notes.append(f"Note: unequal epochs ({epochs}); the guidance is the same number of "
                     "epochs on every side.")
    return notes


def _again(function: Any, args: dict[str, Any]) -> str:
    try:
        return function(**args)
    except Exception as error:  # the report is still written
        return f"(could not be read again: {type(error).__name__}: {error})"


def write_report(tools: Tools, why_no_conclusions: str | None = None) -> Path:
    path = tools.audit.out / "report.md"
    path.write_text(render(tools, why_no_conclusions))
    return path
