"""The L2 audit's final report, ``report.md`` (plan step 3; spec §8, §6.3).

It holds L2's conclusions from ``end_audit`` (quoted, or why there are none), its claims
with the verdict the checker gave each (``claims``), every
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

import glob
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from experiments.l2 import claims as claims_
from experiments.l2.report import SCORER
from experiments.l2.tools import Tools, _ids, _jsonl, post_ref

RECORD = {
    "brief.md": "the brief as given",
    "decisions.jsonl": "L2's choices on what the brief left open, with reasons",
    "proposed.jsonl": "every variant L2 proposed, rejected ones too (with the trace seq)",
    "launches.jsonl": "every launch L2 asked for, with its rationale, and what came of it",
    "trace.jsonl": "every tool call and its (shortened) result, in order",
    "claims.json": "L2's claims, each with its evidence, verdict and the checker's reasons",
    "review.json": "the reviewer's critique of L2's draft, the draft, and L2's response",
    "cosmos.json": "Cosmos links to the claims' runs and messages (experiments.l2.cosmos_links)",
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
    """Distinct comparisons from successful ``scores`` calls with ``against`` (each
    variant against it), first-call order; a and b swapped, or ``rounds`` left out, is
    the same comparison."""
    seen, out = set(), []
    for row in trace:
        if not row.get("ok"):
            continue
        if row.get("tool") == "scores" and row["args"].get("against"):
            pairs = [(a, row["args"]["against"]) for a in _ids(row["args"]["variant_ids"])
                     if a != row["args"]["against"]]
        elif row.get("tool") == "compare":  # audits from before scores had against
            pairs = [(row["args"]["a"], row["args"]["b"])]
        else:
            continue
        for a, b in pairs:
            args = {"a": a, "b": b, "scorer": row["args"]["scorer"],
                    "rounds": row["args"].get("rounds")}
            key = (frozenset((a, b)), args["scorer"], args["rounds"])
            if key not in seen:
                seen.add(key)
                out.append(args)
    return out


def score_tables(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for row in trace:
        if row.get("tool") != "scores" or not row.get("ok") or row["args"].get("against"):
            continue  # a scores call with against is shown as its comparisons
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
    everything = tools.trace()
    trace = [row for row in everything if row.get("by") != "reviewer"]  # L2's own search
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
    out += [f"- Budget: ${brief.budget_usd:.2f}"
            + (f", of which at most ${brief.run_budget_usd:.2f} on runs (the run budget)"
               if brief.run_budget_usd is not None else ""),
            *([f"- Time limit: {audit.time_limit / 60:.1f} min for the whole audit"]
              if audit.time_limit is not None else []),
            f"- Left to L2: {', '.join(open_) or 'none'}", ""]

    out += _claims(tools)
    out += ["## Conclusions (L2's own words, quoted; not checked)", ""]
    if tools.conclusions is not None:
        out += [f"> {line}" if line.strip() else ">" for line in tools.conclusions.splitlines()]
    else:
        out.append(f"None: {why_no_conclusions or 'L2 never called end_audit'}.")
    out.append("")
    out += _review(tools)

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
    out += ["## Variants (what each changed and why), by experiment", ""]
    why = {r["experiment"]: r.get("rationale") for r in launched}
    by_experiment: dict[str, list[dict[str, Any]]] = {exp: [] for exp in why}
    for v in variants:
        by_experiment.setdefault(v["experiment"], []).append(v)
    for exp, rows in by_experiment.items():
        out += [f"### {exp}", "",
                f"Why this experiment (L2's rationale): {_inline(why[exp])}" if why.get(exp)
                else "No rationale for this experiment is recorded in this audit.", ""]
        if not rows:  # e.g. a launch that failed before any variant ran
            out += ["No variant of it ran.", ""]
            continue
        out += ["| variant | base | changes | condition | status | epochs | rationale |",
                "|---|---|---|---|---|---|---|"]
        out += [f"| {v['id']} | {v['base']} | {v['changes']} | {v['condition']} | "
                f"{v['status']} | {v['epochs']} | {_inline(v['rationale'])} |" for v in rows]
        out.append("")
    if not variants:
        out += ["No variant ran.", ""]

    compares, scores = comparisons(trace), score_tables(trace)
    out += ["## Tables the conclusions rest on", "",
            "Every distinct comparison and score table L2 asked for, read again from the logs "
            "(a scores call with against is shown as its comparisons).",
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
    ok_compares = sum(bool(row.get("ok")) and (row.get("tool") == "compare" or (
        row.get("tool") == "scores" and bool(row["args"].get("against")))) for row in trace)
    failed = sum(not row.get("ok") for row in trace)
    by_reviewer = len(everything) - len(trace)
    with_epochs = sum(1 for r in launched if r.get("spent_usd") or any(
        v["epochs"] for v in variants if v["experiment"] == r["experiment"]))
    out += ["## Search counts (from the record)", "",
            f"- Variants proposed: {len(proposed)} ({outcomes['run']} run, "
            f"{outcomes['rejected']} rejected, {outcomes['not run']} not run)",
            f"- Experiments launched: {len(launched)} ({with_epochs} with at least one epoch "
            f"done); launch attempts: {len(launches)}",
            f"- Scorers read: {len(scorers)} ({', '.join(scorers) or 'none'})",
            f"- Comparisons tried: {len(compares)} distinct ({ok_compares} successful calls)",
            f"- Transcripts read: {calls['transcript']}; searches: {calls['find']}; "
            f"code runs: {calls['run_python']} (the code and its output are in trace.jsonl)",
            f"- Tool calls: {len(trace)} ({failed} refused or failed)"
            + (f", and {by_reviewer} by the reviewer (not counted above)" if by_reviewer
               else ""), "",
            "These count L2's own calls; the reviewer's are listed under Review.", ""]

    out += ["## Departures from the reading guidance (automatic checks)", ""]
    notes = []
    if tools.conclusions is not None and not compares:
        notes.append("- No comparison was made (scores was never called with against), "
                     "so no claim that a change made a difference rests on one.")
    for args in compares:
        try:
            found = tools.departures(args["a"], args["b"], args["scorer"], args["rounds"])
        except Exception as error:
            found = [f"could not check: {type(error).__name__}: {error}"]
        notes += [f"- compare {args['a']} vs {args['b']} ({args['scorer']}): {n}"
                  for n in [*found, *_unknown_answers(tools, [args["a"], args["b"]],
                                                      args["scorer"])]]
    for args in scores:
        notes += [f"- scores {', '.join(args['variant_ids'])} ({args['scorer']}): {n}"
                  for n in [*_score_departures(tools, args["variant_ids"]),
                            *_unknown_answers(tools, args["variant_ids"], args["scorer"])]]
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
    if b["run_budget_usd"] is not None:
        out.append(f"- Runs: ${b['by_kind']['experiments']:.4f} of the "
                   f"${b['run_budget_usd']:.2f} run budget (a launched epoch runs to completion, "
                   "so runs can go over it by up to one epoch)")
    out += [f"- {name}: ${usd:.4f}" for name, usd in b["by_experiment"].items()]
    out += [f"- L2 on {model}: ${row.get('usd', 0):.4f} ({row.get('calls', '?')} calls, "
            f"{row.get('input_tokens', '?')} input / {row.get('output_tokens', '?')} output "
            "tokens)" for model, row in b["l2_by_model"].items()]
    if tools.review is not None:
        out.append(f"- Of L2's own calls, the reviewer's: ${tools.review.get('cost_usd') or 0:.4f}")
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


def _inline(value: Any) -> str:
    """L2's text on one line, its markdown escaped, so it cannot add or fake report
    structure (a heading, a list item, a bold verdict)."""
    text = " ".join(str(value).split())
    return re.sub(r"([\\`*_\[\]<>#|])", r"\\\1", text)


def _ref(ref: dict[str, Any]) -> str:
    if "call" in ref:
        return f"call {ref['call']}"
    quote = f' ("{_inline(ref["quote"])}")' if ref.get("quote") else ""
    return (f"{_inline(ref['variant'])} epoch {ref['epoch']} post "
            f"{_inline(ref['post'])}{quote}")


def _claims(tools: Tools) -> list[str]:
    """L2's claims, most doubtful verdicts first, each with what it cites and why it got
    its verdict."""
    out = ["## Claims, checked against the logs", ""]
    if tools.claims is None:
        refused = sum(row.get("tool") == "end_audit" and not row.get("ok")
                      and (row.get("args") or {}).get("claims") is not None
                      for row in tools.trace())
        why = (f"L2's claims were refused {refused} time(s) (see trace.jsonl) and none were "
               "recorded" if refused else "L2 gave no claims")
        return out + [f"{why}, so nothing it concluded was checked against the logs; read "
                      "its conclusions as unchecked.", ""]
    if not tools.claims:
        return out + ["L2 claimed nothing.", ""]
    out += [f"{claims_.summary(tools.claims)}. A claim is *checked* only if the logs, "
            "read again, bear it out (a comparison also needs an accepted measure and no "
            "departure from the reading guidance); a *lead* falls short; the rules are in "
            "spec §6.4. Anything L2 wrote outside its claims is not checked.", ""]
    links = _cosmos(tools)
    if links:
        out += ["Each claim links to its evidence on Cosmos: the messages it quotes, and every "
                "run its counts come from (✓ the target showed the behaviour, ✗ it did not, "
                "? unknown).", ""]
    order = {v: i for i, v in enumerate(("contradicted", "invalid", "lead", "unchecked",
                                         "checked"))}
    for i, claim in sorted(enumerate(tools.claims, 1),
                           key=lambda pair: order.get(pair[1]["verdict"], 0)):
        out += claim_lines(i, claim)
        try:
            out += [f"   - {line}" for line in _claim_links(tools, claim, links)]
        except Exception as error:  # the report is still written
            out.append(f"   - (Cosmos links could not be made: {_inline(error)})")
    try:
        where = _where(tools)
    except Exception as error:  # the report is still written
        where = ["### Where the cited evidence is", "",
                 f"(could not be worked out: {_inline(f'{type(error).__name__}: {error}')})",
                 ""]
    return out + ["", *where]


def claim_lines(i: int, claim: dict[str, Any]) -> list[str]:
    """Claim ``i`` with its verdict, fields, evidence and the checker's reasons."""
    fields = ", ".join(f"{name}={_inline(claim[name])}"
                       for name in claims_.FIELDS[claim["kind"]])
    return [f"{i}. **{claim['verdict']}** ({claim['kind']}{': ' + fields if fields else ''})"
            f": {_inline(claim['text'])}",
            f"   - Evidence: {', '.join(_ref(r) for r in claim['evidence']) or 'none'}",
            *[f"   - {_inline(reason)}" for reason in claim["reasons"]]]


def _review(tools: Tools) -> list[str]:
    """The reviewer's critique of L2's draft and L2's response (``review.json``)."""
    review = tools.review
    if review is None:
        return (["## Review", "", "No review was requested: L2 did not call request_review "
                 "before the audit stopped.", ""] if tools.needs_review else [])
    calls = review.get("reviewer_calls") or []
    out = ["## Review (an adversarial reviewer read L2's draft before the audit ended)", "",
           f"L2 asked for the review in call {review['call']}. The reviewer saw the draft "
           "claims with the checker's verdicts and could read the runs (list_runs, scores, "
           f"transcript, find) but not run anything; it made {len(calls)} call(s)"
           + (f" ({', '.join(f'call {c}' for c in calls)})" if calls else "")
           + f" and cost ${review.get('cost_usd') or 0:.4f}, part of L2's own calls in the "
           "ledger." + (f" It stopped early: {_inline(review['stopped'])}."
                         if review.get("stopped") else ""), "",
           "### L2's draft claims, as the checker read them", ""]
    draft = review["draft"]
    out += [f"{i}. **{c['verdict']}** ({c['kind']}): {_inline(c['text'])}"
            for i, c in enumerate(draft.get("claims") or [], 1)] or ["L2 claimed nothing."]
    out += ["", "The draft conclusions are in `review.json`.", "",
            "### The critique (the reviewer's words, quoted; not checked)", ""]
    out += _quoted(review.get("critique")) or ["The reviewer gave no critique."]
    out += ["", "### L2's response (its words, quoted; not checked)", ""]
    out += _quoted(review.get("response")) or ["None: L2 did not end the audit after the "
                                               "review."]
    return out + [""]


def _quoted(text: Any) -> list[str]:
    return [f"> {line}" if line.strip() else ">" for line in str(text or "").strip()
            .splitlines()]


def _cosmos(tools: Tools) -> dict[str, Any]:
    """The Cosmos links ``experiments.l2.cosmos_links`` saved for this audit, if any."""
    try:
        links = json.loads((tools.audit.out / "cosmos.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(links, dict):
        return {}
    url = re.compile(r"https?://[^\s()<>\[\]`#]+")
    found = {k: {str(name): u for name, u in part.items()
                 if url.fullmatch(str(u).split("#msg-", 1)[0])
                 and re.fullmatch(r"\d*", str(u).partition("#msg-")[2])}
             for k in ("runs", "samples", "posts")
             for part in [links.get(k) if isinstance(links.get(k), dict) else {}]}
    runs = links.get("messages") if isinstance(links.get("messages"), dict) else {}
    found["messages"] = {
        str(name): {"url": run["url"], "seq": {str(m): q for m, q in run["seq"].items()
                                               if type(q) is int}}
        for name, run in runs.items()
        if isinstance(run, dict) and url.fullmatch(str(run.get("url")))
        and isinstance(run.get("seq"), dict)}
    return found


def _claim_links(tools: Tools, claim: dict[str, Any], links: dict[str, Any]) -> list[str]:
    """A claim's evidence as Cosmos links: each cited message, and for a rate or a
    comparison each run of its variants, marked with what the target did there."""
    if not links:
        return []
    out = []
    posts = [(r, links["posts"].get(f"{r['variant']}/{r['epoch']}/{r['post']}"))
             for r in claim["evidence"] if "call" not in r]
    shown = [f"[{_inline(r['variant'])} epoch {r['epoch']} post {_inline(r['post'])}]({u})"
             for r, u in posts if u]
    if shown:
        out.append("On Cosmos: " + ", ".join(shown))
    if claim["kind"] not in ("rate", "difference", "no_difference"):
        return out
    ids = [claim["variant"]] if claim["kind"] == "rate" else [claim["a"], claim["b"]]
    used = claims_.basis(tools, claim)  # the call the verdict was read from
    if used is None:
        return out
    results = tools.results()
    for vid in ids:
        cells = []
        for sample in sorted(results.of(vid), key=lambda s: s.epoch):
            try:
                did = results.adopted(sample, claim["target"], used["scorer"], used["rounds"],
                                      used["field"])
            except ValueError:
                did = None
            mark = "?" if did is None else ("✓" if did else "✗")
            url = links["samples"].get(f"{vid}/{sample.epoch}")
            cells.append(f"[{sample.epoch} {mark}]({url})" if url else
                         f"{sample.epoch} {mark} (not on Cosmos)")
        if cells:
            out.append(f"On Cosmos, {_inline(claim['target'])} in each run of "
                       f"{_inline(vid)}, as call {used['call']} read it (epoch, then ✓/✗): "
                       + " ".join(cells))
        if used["scorer"] == "counts":
            out += _counted(tools, claim["target"], vid, used, links)
    return out


def _counted(tools: Tools, target: str, vid: str, used: dict[str, Any],
             links: dict[str, Any]) -> list[str]:
    """Every message a ``counts`` claim counted, per run: the target's posts that use the
    scenario's name in its own words (copies of others' posts do not count), linked to
    the message on Cosmos. A run is listed only when its recorded count shows the
    behaviour and the posts found add up to that count; otherwise it says so."""
    from experiments.l2.results import own_uses
    from experiments.measures import own_mentions_by_post

    if used["rounds"] is not None:
        return [f"(the messages are not listed for a count up to round {used['rounds']}; "
                "open the runs above)"]
    results, runs, off = tools.results(), [], []
    for sample in sorted(results.of(vid), key=lambda s: s.epoch):
        recorded = own_uses(sample, target)
        if not recorded:  # no use recorded, or the count is unknown: nothing was counted
            continue
        try:
            pattern = results.scenario(sample.base).mention
            found = [(p, hits) for p, hits in own_mentions_by_post(
                pattern, target, tools.posts(sample)) if hits > 0]
        except Exception:  # an unreadable log: this run only
            off.append(f"epoch {sample.epoch} (its log could not be read)")
            continue
        if sum(hits for _, hits in found) != recorded:
            off.append(f"epoch {sample.epoch} (recorded {recorded} uses; the posts found have "
                       f"{sum(hits for _, hits in found)})")
            continue
        run = links.get("messages", {}).get(f"{vid}/{sample.epoch}")
        cells = []
        for post, _ in found:
            seq = (run or {}).get("seq", {}).get(str(post.get("id")))
            label = f"post {_inline(post_ref(post))}"
            cells.append(f"[{label}]({run['url']}#msg-{seq})" if run and seq is not None
                         else f"{label} (not on Cosmos)")
        runs.append(f"epoch {sample.epoch}: " + ", ".join(cells))
    out = []
    if runs:
        out.append(f"The messages counted in {_inline(vid)} ({_inline(target)}'s own uses of "
                   "the name): " + "; ".join(runs))
    if off:
        out.append(f"Messages not listed in {_inline(vid)}: " + "; ".join(off))
    return out


def _code(value: Any) -> str:
    """A value in a code span, or escaped text if it would break one."""
    text = " ".join(str(value).split())
    return _inline(text) if "`" in text else f"`{text}`"


def _where(tools: Tools) -> list[str]:
    """Where each cited call and post is, once each, so a reader can check a claim by
    hand: the trace row, the eval logs it read (paths from the audit directory), and
    the name its sample has on Cosmos (the run of that experiment, once uploaded)."""
    refs = [r for claim in tools.claims or [] for r in claim["evidence"]]
    calls = sorted({r["call"] for r in refs if "call" in r})
    posts = list(dict.fromkeys((r["variant"], r["epoch"], r["post"]) for r in refs
                               if "call" not in r))
    if not calls and not posts:
        return []
    out = ["### Where the cited evidence is", "",
           "Paths are from the audit directory. Open a log with `inspect view` or on Cosmos, "
           "where each experiment is one run (`python -m experiments.l2.cosmos`) and each "
           "sample is filed under its variant's built name, condition and epoch. Cosmos "
           "numbers messages itself, so find a post there by its text (or its message id "
           "in the log), not by its post number here.", ""]
    rows = {row.get("seq"): row for row in tools.trace()}
    for seq in calls:
        row = rows.get(seq) or {}
        args = row.get("args") or {}
        shown = ", ".join(f"{k}={_inline(json.dumps(v, default=str))}"
                          for k, v in sorted(args.items()) if v is not None and k != "code")
        out.append(f"- **call {seq}**: `{row.get('tool')}` ({shown}), row {seq} of "
                   "`trace.jsonl`" + (" (its code is in the row's args)"
                                      if row.get("tool") == "run_python" else ""))
        try:
            read = _variants_read(tools, row)
        except Exception as error:
            out.append(f"  - (the variants it read could not be worked out: "
                       f"{_inline(error)})")
            continue
        out += [f"  - {line}" for line in _logs(tools, read)]
    for variant, epoch, post in posts:
        out.append(f"- **{_inline(variant)} epoch {epoch} post {_inline(post)}**: "
                   + _post_where(tools, {"variant": variant, "epoch": epoch, "post": post}))
    return out + [""]


def _variants_read(tools: Tools, row: dict[str, Any]) -> list[str]:
    args = row.get("args") or {}
    if row.get("tool") == "scores":
        return list(dict.fromkeys([*_ids(args.get("variant_ids") or []),
                                   *([args["against"]] if args.get("against") else [])]))
    if row.get("tool") == "transcript":
        return [args["variant_id"]] if args.get("variant_id") else []
    if row.get("tool") == "find":
        ids = args.get("variant_ids")
        if ids:
            return _ids(ids)
        # every variant there was when it ran: those launched before this call
        launched = claims_._launch_seqs(tools)
        return sorted(v for v in tools.results().runs
                      if launched.get(v, 0) < row.get("seq", 0))
    return []


def _logs(tools: Tools, ids: list[str]) -> list[str]:
    """For each variant: its experiment, its eval logs and its name on Cosmos."""
    lines = []
    try:
        results = tools.results()
    except Exception as error:  # the report is still written
        return [f"(the logs could not be read: {error})"]
    for vid in ids:
        samples = sorted(results.of(vid), key=lambda s: s.epoch)
        if not samples:
            lines.append(f"{_code(vid)}: no runs found")
            continue
        paths = sorted({_rel(tools, s.log) for s in samples})
        if len(paths) == 1:
            where = _code(paths[0])
        else:
            pattern = _pattern(paths)
            root = tools.audit.out
            matched = {_rel(tools, str(m)) for m in root.glob(pattern)} if not Path(
                pattern).is_absolute() else set(glob.glob(pattern))
            # a pattern that would also match another variant's logs (e.g. ids V_x and
            # V-x, which Inspect's file names make the same) is replaced by the list
            where = (f"{len(paths)} logs, one per epoch: {_code(pattern)}"
                     if matched == set(paths) else
                     f"{len(paths)} logs: " + ", ".join(_code(p) for p in paths))
        built = f"{samples[0].base}--{vid}"
        run = _cosmos(tools).get("runs", {}).get(tools._where.get(vid, ""))
        if run:
            where += f"; [its Cosmos run]({run})"
        lines.append(f"{_code(vid)} ({tools._where.get(vid, '?')}, epochs "
                     f"{_span([s.epoch for s in samples])}): {where}; on Cosmos, "
                     f"{_code(built)} / {_code(samples[0].condition)}")
    return lines


def _post_where(tools: Tools, ref: dict[str, Any]) -> str:
    try:
        post = claims_.find_post(tools, ref)
        sample = tools._sample(ref["variant"], ref["epoch"])
    except Exception as error:
        return f"(could not be found: {_inline(error)})"
    text = " ".join(str(post.get("content", "")).split())
    built = f"{sample.base}--{ref['variant']}"
    url = _cosmos(tools).get("posts", {}).get(f"{ref['variant']}/{ref['epoch']}/{ref['post']}")
    return ((f"[open the message on Cosmos]({url}); " if url else "")
            + f"{_code(_rel(tools, sample.log))}, message {_code(post.get('id'))} (turn "
            f"{post.get('turn')}, #{_inline(post.get('channel_id'))}, "
            f"{_inline(post.get('label'))}); on Cosmos, {_code(built)} / "
            f"{_code(sample.condition)} / epoch {ref['epoch']}. It reads: "
            f"\"{_inline(text[:300])}{'…' if len(text) > 300 else ''}\"")


def _rel(tools: Tools, path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(tools.audit.out.resolve()))
    except ValueError:  # an analysis-only audit reads another audit's logs
        return str(path)


def _pattern(paths: list[str]) -> str:
    """One glob for a variant's per-epoch logs: ``…/logs/epoch-*/<time>_<task>_<id>.eval``
    becomes ``…/logs/epoch-*/*_<task>_*.eval``."""
    first = Path(paths[0])
    task = first.name.split("_", 1)[1].rsplit("_", 1)[0] if first.name.count("_") >= 2 else "*"
    return str(first.parent.parent / "epoch-*" / f"*_{task}_*.eval")


def _span(epochs: list[int]) -> str:
    epochs = sorted(set(epochs))
    if epochs == list(range(epochs[0], epochs[-1] + 1)) and len(epochs) > 2:
        return f"{epochs[0]}–{epochs[-1]}"
    return ", ".join(map(str, epochs))


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


def _unknown_answers(tools: Tools, ids: list[str], scorer: str) -> list[str]:
    """A probe read where most answers are unknown (UNPARSED, or no answer at all)."""
    if scorer != "probe":
        return []
    try:
        results = tools.results()
        unknown = {(vid, agent): sum(results.adopted(s, agent, "probe") is None
                                     for s in results.of(vid))
                   for vid in ids for agent in results.targets(vid)}
        asked = {(vid, agent): len(results.of(vid)) for vid, agent in unknown}
    except Exception as error:
        return [f"could not check: {type(error).__name__}: {error}"]
    total, answers = sum(unknown.values()), sum(asked.values())
    if not answers or total * 2 <= answers:
        return []
    per = ", ".join(f"{vid} {agent} {unknown[vid, agent]}/{asked[vid, agent]}"
                    for vid, agent in unknown)
    return [f"Note: {total} of {answers} probe answers are unknown (unparsed or missing: "
            f"{per}), so conclusions resting on this probe are thin."]


def _again(function: Any, args: dict[str, Any]) -> str:
    try:
        return function(**args)
    except Exception as error:  # the report is still written
        return f"(could not be read again: {type(error).__name__}: {error})"


CLAIMS = "## Claims, checked against the logs"
NEXT = "## Conclusions"


def relink(out: Path) -> Path:
    """Write the claims section of an ended audit's ``report.md`` again, e.g. once its
    Cosmos links are resolved; the rest of the report is kept as it is, byte for byte."""
    from experiments.l2.audit import Audit
    from experiments.l2.brief import parse_brief

    out = Path(out)
    report = out / "report.md"
    text = report.read_text()
    start, end = text.find(CLAIMS), text.find("\n" + NEXT)
    if start < 0 or end < start:
        raise ValueError(f"{report} has no claims section to write again")
    tools = Tools(Audit(out, parse_brief(out / "brief.md")))
    if (out / "claims.json").exists():
        tools.claims = json.loads((out / "claims.json").read_text())
    section = "\n".join(_claims(tools))
    report.write_text(text[:start] + section.rstrip("\n") + "\n" + text[end:])
    return report


def write_report(tools: Tools, why_no_conclusions: str | None = None) -> Path:
    path = tools.audit.out / "report.md"
    path.write_text(render(tools, why_no_conclusions))
    return path
