"""The milestone 1 report: the grid's comparisons, read from an audit's eval logs.

    uv run python -m experiments.l2.report logs/l2/<audit>

For each "Compared with" pair of the spec's §10 table, a per-target k/n table:
``mfa_rumor`` on the private probe (NOT_REQUIRED adopts the claim; an end-of-run
interview, so rounds variants are compared on the whole run); ``kestrel_naming``
on at least one use of the name in the target's own words, compared at the rounds
the two variants share when their rounds differ, with the whole-run figure shown
alongside as recorded, not compared (spec §6.3). Then a sanity check (M0 vs M6:
they differ only by an idle Advocate and the helper's wider scope, so a large gap
is a red flag in the setup, never a finding), the seat-dose flags, warnings,
rejections and errors, cost, and how many variants, scorers and comparisons were
tried (spec §6.3).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from experiments.l2.results import Results

PAIRS: tuple[tuple[str, str], ...] = (
    ("M1", "M0"), ("M2", "M0"), ("M3", "M0"), ("M4", "M0"), ("M5", "M0"), ("M4", "M5"),
    ("M7", "M6"),
    ("K2", "K1"), ("K3", "K1"), ("K4", "K1"), ("K5", "K1"), ("K6", "K1"), ("K7", "K1"),
    ("K6", "K7"),
)
"""Each variant and the control it is compared with (spec §10, "Compared with")."""
SANITY = ("M0", "M6")
RED_FLAG_GAP = 0.5
"""A difference in rates of at least this many (50 percentage points) between M0 and M6
is flagged."""
SCORER = {"mfa_rumor": "probe", "kestrel_naming": "counts"}
MEASURE = {"probe": "probe NOT_REQUIRED (adopts the claim)",
           "counts": "uses the name in its own words"}


def _rate(side: dict[str, Any]) -> str:
    text = f"{side['k']}/{side['n']}"
    return text + (f" (+{side['incomplete']} incomplete)" if side["incomplete"] else "")


def comparison(results: Results, a: str, b: str) -> tuple[list[str], dict[str, Any]]:
    """The table for one pair, and the comparison it made (for the search counts)."""
    base = results.base(a)
    scorer = SCORER[base]
    shared = None
    if scorer == "counts":
        # From the variants' definitions, never their scores: if round_counts is missing,
        # those samples are incomplete, never compared on the whole run instead.
        ra, rb = results.rounds(a), results.rounds(b)
        if ra != rb:
            if ra is None or rb is None:
                raise ValueError(f"{a} and {b} differ in schedule but not in rounds")
            shared = min(ra, rb)
    compared = results.compare(a, b, scorer, rounds=shared)
    head = f"### {a} vs {b} ({base}): {MEASURE[scorer]}"
    if shared is not None:
        head += f", compared at the {shared} round(s) both have"
    lines = [head, ""]
    sizes = {vid: len(results.epochs(vid)) for vid in (a, b)}
    if sizes[a] != sizes[b]:
        lines += [f"WARNING: unequal epochs ({a}: {sizes[a]}, {b}: {sizes[b]}); a comparison "
                  "needs the same number of epochs on both sides (spec §6.3).", ""]
    difference = f"difference ({a} − {b})"
    if shared is not None:
        whole = results.compare(a, b, scorer)
        lines += [f"| target | {a} | {b} | {difference} | {a} whole run | {b} whole run |",
                  "|---|---|---|---|---|---|"]
    else:
        lines += [f"| target | {a} | {b} | {difference} |", "|---|---|---|---|"]
    for agent, row in compared.items():
        diff = "n/a" if row["difference"] is None else f"{row['difference']:+.2f}"
        cells = [agent, _rate(row["a"]), _rate(row["b"]), diff]
        if shared is not None:
            cells += [_rate(whole[agent]["a"]), _rate(whole[agent]["b"])]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines, {"pair": [a, b], "scorer": scorer, "rounds": shared, "table": compared}


def report(audit: str | Path) -> str:
    """The report for the audit directory ``audit``."""
    audit = Path(audit)
    cost = json.loads((audit / "cost.json").read_text()) if (audit / "cost.json").exists() else {}
    decisions = (json.loads((audit / "decisions.json").read_text())
                 if (audit / "decisions.json").exists() else {})
    return render(Results.load(audit), cost, decisions, audit.name)


def render(results: Results, cost: dict[str, Any], decisions: dict[str, Any],
           name: str) -> str:
    ran = {s.variant_id for s in results.samples}
    out = [f"# Level 2 milestone 1 report: {name}", ""]
    epochs = {vid: len(results.epochs(vid)) for vid in sorted(ran)}  # from the logs
    out += [f"Epochs per variant (from the logs): {sorted(set(epochs.values())) or 'none'}"
            + ("" if len(set(epochs.values())) <= 1 else f" (unbalanced: {epochs})"), ""]
    if decisions.get("why_not_launched"):
        out += [f"Not launched: {decisions['why_not_launched']}", ""]
    if decisions.get("stopped"):
        out += [f"Stopped: {decisions['stopped']}", ""]
    if failed := decisions.get("failed_epoch"):
        spent = sum(v for v in failed["logs"].values() if isinstance(v, float))
        out += [f"Failed epoch {failed['epoch']}: {len(failed['logs'])} log(s) left on disk, "
                f"${spent:.4f} spent; not used in any table.", ""]

    made, skipped = [], []
    out += ["## Comparisons", ""]
    for a, b in PAIRS:
        if a not in ran or b not in ran:
            missing = [vid for vid in (a, b) if vid not in ran]
            skipped.append(f"{a} vs {b}: {' and '.join(missing)} did not run")
            continue
        lines, record = comparison(results, a, b)
        out += lines
        made.append(record)
    if skipped:
        out += ["Not compared:", *[f"- {s}" for s in skipped], ""]

    out += ["## Sanity check (a red flag in the setup, never a finding)", ""]
    a, b = SANITY
    if a in ran and b in ran:
        lines, record = comparison(results, a, b)
        out += lines[2:]  # the table, under this section's heading
        table = record["table"]
        flagged = [agent for agent, row in table.items()
                   if row["difference"] is not None and abs(row["difference"]) >= RED_FLAG_GAP]
        unchecked = [agent for agent, row in table.items() if row["difference"] is None]
        gap = f"{RED_FLAG_GAP * 100:.0f} percentage points"
        if flagged:
            out.append(f"RED FLAG: {a} and {b} differ by {gap} or more for {flagged}; "
                       "check the setup (they should behave alike).")
        elif not unchecked:
            out.append(f"{a} and {b} agree within {gap} for every target.")
        else:
            out.append(f"{a} and {b} agree within {gap} where they could be compared.")
        if unchecked:
            out.append(f"Not checked for {unchecked}: no complete samples on one side.")
        out.append("")
    else:
        out += [f"{a} or {b} did not run.", ""]

    out += ["## Seat dose (did Alice deliver the intended dose?)", ""]
    doses = [(s.variant_id, s.epoch, s.scores.get("seat_dose"),
              s.why.get("seat_dose") or ("no score (the sample errored?)"
                                         if "seat_dose" not in s.scores else None))
             for s in sorted(results.samples, key=lambda s: (s.variant_id, s.epoch))
             if results.intended_dose(s.variant_id) is not None]
    off = [(vid, epoch, dose, why) for vid, epoch, dose, why in doses
           if dose is None or not dose["on_dose"]]
    out += [f"{len(doses) - len(off)} of {len(doses)} dosed samples on dose. Off-dose samples "
            "are flagged and kept in every table above."]
    for vid, epoch, dose, why in off:
        out.append(f"- {vid} epoch {epoch}: " + (
            f"unknown ({why})" if dose is None else
            f"intended {dose['intended']}, delivered {dose['delivered'] or 'no dose'}, "
            f"opening delivered "
            f"{dose['opening_delivered']}, own mentions {dose['own_mentions']}"
            + (f" ({dose['why']})" if dose.get("why") else "")))
    out.append("")

    out += ["## Problems", ""]
    out += [f"- {problem}" for problem in results.problems]
    for vid, row in results.variants.items():
        for kind in ("rejections", "warnings"):
            out += [f"- {vid} {kind[:-1]}: {text}" for text in row.get(kind, [])]
    for vid, run in results.runs.items():
        out += [f"- {vid} error: {text}" for text in run.get("errors", [])]
    incomplete = sorted({(s.variant_id, s.epoch, name, why) for s in results.samples
                         for name, why in s.why.items()})
    out += [f"- {vid} epoch {epoch} incomplete {name}: {why}"
            for vid, epoch, name, why in incomplete]
    if out[-1] == "":
        out.append("None.")
    out.append("")

    out += ["## Cost", ""]
    if cost:
        out += [f"Spent ${cost['spent_usd']:.4f} of ${cost['budget_usd']:.2f} (prices checked "
                f"{cost['prices_checked']}; estimate ${cost['estimate']['usd']:.4f} from "
                f"{', '.join(cost['estimate']['basis']) or 'nothing'})."]
        out += [f"- {vid}: ${usd:.4f}" for vid, usd in sorted(cost["by_variant"].items())]
        out += [f"- epoch {e['epoch']}: ${e['usd']:.4f}" + (" (failed)" if e.get("failed")
                                                           else "")
                for e in cost.get("by_epoch", [])]
        if cost.get("note"):
            out.append(f"Note: {cost['note']}.")
    else:
        out.append("No cost.json in the audit directory.")
    out.append("")

    scorers = sorted({name for s in results.samples for name in s.scores})
    out += ["## Search counts", "",
            f"Variants tried: {len(results.variants)} ({len(ran)} run, "
            f"{len(results.variants) - len(ran)} not run). Scorers: {len(scorers)} "
            f"({', '.join(scorers)}). Comparisons: {len(made)} (plus the sanity check).", ""]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("audit", type=Path, help="an audit directory from experiments.l2")
    parser.add_argument("--out", type=Path, help="also write the report here (Markdown)")
    args = parser.parse_args()
    text = report(args.audit)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
