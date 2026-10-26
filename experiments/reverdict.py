"""Recompute reviewer verdicts for saved release-pressure results from their Inspect logs.

    uv run python -m experiments.reverdict logs/live/<run>

Verdict extraction improved after a sweep; this re-reads each sample's final
answer from the logs instead of re-running the audits.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from inspect_ai.log import read_eval_log, resolve_sample_attachments

from experiments.run import JUDGE, _fresh, judge_verdict, reviewer_said


async def main(run: Path) -> None:
    results_path = run / "results.json"
    data = json.loads(results_path.read_text())
    judge = _fresh(JUDGE)
    for row in data["results"]:
        if row.get("base", row["scenario"]) != "release_pressure":  # variants: <base>--<id>
            continue
        log = read_eval_log(row["log"])
        sample = next(s for s in log.samples if s.epoch == row["epoch"])
        sample = resolve_sample_attachments(sample)
        texts = [*row["agents"]["reviewer"]["posts"],
                 sample.output.completion if sample.output else ""]
        before, row["verdict"] = row.get("verdict"), await judge_verdict(judge, texts)
        row["reviewer_said"] = reviewer_said(texts)
        print(row["condition"], row["epoch"], before, "->", row["verdict"])
    results_path.write_text(json.dumps(data, indent=2, default=str))


if __name__ == "__main__":
    load_dotenv(".env")
    asyncio.run(main(Path(sys.argv[1])))
