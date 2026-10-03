"""Render the team report from seed-audit results.

    uv run python -m experiments.report [logs/live/<run>/results.json] [--out PATH]

Without results (or with mock results) the report shows the seed-audit design
and a pending note instead of charts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

TEMPLATE = Path(__file__).with_name("report_template.html")


def slim(results: dict[str, Any]) -> dict[str, Any]:
    """Drop bulky evidence the page does not render."""
    rows = []
    for row in results.get("results", []):
        row = dict(row)
        row["interventions"] = {k: v for k, v in row["interventions"].items() if k != "records"}
        turns = row["auditor"].get("turns") or []
        row["auditor"] = {**row["auditor"], "turns": [
            {**turn, "said": (turn.get("said") or "")[:600]} for turn in turns]}
        rows.append(row)
    return {**results, "results": rows}


def render(results: dict[str, Any] | None) -> str:
    payload = json.dumps(slim(results) if results else None, default=str)
    return TEMPLATE.read_text().replace("__RESULTS__", payload.replace("</", "<\\/"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", nargs="?")
    parser.add_argument("--out", default="logs/report/orbit-auditor-report.html")
    args = parser.parse_args()
    results = json.loads(Path(args.results).read_text()) if args.results else None
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(results))
    print(out)


if __name__ == "__main__":
    main()
