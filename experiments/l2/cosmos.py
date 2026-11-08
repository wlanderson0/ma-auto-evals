"""Export an L2 experiment as a folder ``cosmos upload`` accepts (spec §8.1).

Usage::

    uv run python -m experiments.l2.cosmos <experiment dir> --out <folder> [--pr N] [--name NAME]

``<experiment dir>`` is any directory with ``runs.jsonl`` and ``logs/epoch-<k>/*.eval``:
an L2 audit's ``experiments/exp-<n>``, or the milestone 1 CLI's ``--out``. It is only
read, never changed. ``<folder>`` must be new or empty. The command ends by printing
the ``cosmos upload`` command line; it never uploads.

One experiment is one Cosmos run; each of its samples is one L2 run. The folder holds:

- ``<built scenario name>/<condition>/<log>.eval``: a copy of each log under the
  variant's built name (``<base>--<variant id>``) and inner condition, the two folder
  names Cosmos files a sample under. Inspect's file names are kept; a clash fails, never
  overwrites. Each log holds one L2 epoch, so Inspect's ``sample.epoch`` is always 1;
  Cosmos takes the epoch from the log and keeps one sample per (scenario, condition,
  epoch, sample id), so the copy's ``sample.epoch`` is set to the L2 epoch
  (``l2.epoch`` in the task metadata). Nothing else in the copy changes.
- ``results.json``: Cosmos's input, written only by this export. It is not the
  ``results.json`` ``experiments.run`` writes and the spec is retiring (§5, §6): L2
  reads its results from the logs. ``{"results": [row, ...]}``, one row per sample.
  Row fields, from Cosmos's parser (``cosmos.ingest.parse``: ``match_results``,
  ``_read_json_files``; ``cosmos.ingest.load``: ``scenario_seats``,
  ``check_off_seat``) and from ``experiments.run.measure``:

  - ``scenario``, ``condition``, ``epoch``: the sample's key in Cosmos (the two folder
    names and the epoch in the copied log); ``log``: the copied log's file name, which
    Cosmos matches first, so each row pairs with its own log;
  - ``seat``: the agent the seat's opening is written as (Cosmos's off-seat check);
  - ``variant_id``, ``base``, ``error``: shown on the sample page, as every field is;
  - ``agents``: per target, its value for each of the scenario's scorers
    (``post_judge``; ``outcome`` when it has an outcome function; ``probe`` when it has
    probes), as ``experiments.l2.results.Results.scores`` gives them (``incomplete``
    when a value could not be made).

  ``interventions.off_seat``, which Cosmos cross-checks when present, is left out: L2's
  inner conditions have no auditor whose interventions it would count.
- ``manifest.json``: from ``experiments.run.build_manifest``, with each variant's
  built scenario, so per-agent models are as run (written scenarios and ``set_model``
  included). It describes the run, not the export: the commit, the dirty flag and the
  start and end times come from the experiment's ``decisions.json`` (the branch only
  when the checkout is at that commit, else null), as do ``run_config``, the settings
  the runs' models had, and ``without_run_config``, the variants whose agents ran on
  several models and so without them (extra fields; Cosmos keeps the manifest as given).
  ``--pr`` decides which PR Cosmos files the run under. For an experiment of an L2 audit,
  ``study: {id, name, experiment}`` names the audit's study and the experiment's
  ``exp-<n>`` (``experiments.l2.study``), so Cosmos groups the audit's runs, and
  ``study.probes`` gives each variant's probes as its built scenario has them:
  ``{"<variant id>": {"<target agent>": {"question", "options", "adopted"}}}``, where
  ``adopted`` is the answer that counts as adopting (Cosmos recounts probe claims with
  it). A variant without probes is left out; with none at all, so is ``probes``.
- For an experiment of an L2 audit (the directory two levels up has ``brief.md`` and
  ``report.md``): ``report.html`` (Cosmos's "Open report"), the audit's report followed
  by its brief, the experiment's variants (``variants.jsonl``, one record each) and the
  code and hash of each written scenario it ran. ``cosmos upload`` sends only ``.eval``,
  ``.json`` and ``.html`` files, so these are in the report rather than copied as files.
"""

from __future__ import annotations

import argparse
import html
import json
import shlex
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from inspect_ai.log import read_eval_log, write_eval_log
from markdown_it import MarkdownIt

from experiments.l2 import experiment as m1
from experiments.l2 import written as written_
from experiments.l2.results import Results
from experiments.l2.study import study_of
from experiments.run import build_manifest, write_manifest
from experiments.scenarios import Scenario


def scorers(scenario: Scenario) -> tuple[str, ...]:
    """The per-target scorers a row carries for ``scenario``."""
    return (("post_judge",) + (("outcome",) if scenario.outcome is not None else ())
            + (("probe",) if scenario.probes else ()))


def audit_of(experiment: Path) -> Path | None:
    """The L2 audit directory ``experiment`` belongs to, if it is one's."""
    audit = Path(experiment).resolve().parent.parent
    return audit if (audit / "brief.md").is_file() and (audit / "report.md").is_file() else None


def export(experiment: Path, out: Path, *, pr: int | None = None,
           argv: Sequence[str] = ()) -> Path:
    """Write the Cosmos folder for ``experiment`` to ``out``; return ``out``."""
    experiment, out = Path(experiment), Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty")
    if out.resolve().is_relative_to(experiment.resolve()):
        raise ValueError(f"{out} is inside {experiment}, which the export never changes")
    audit = audit_of(experiment)
    written = written_.load(audit / "scenarios") if audit is not None else written_.NONE
    results = Results.load(experiment, written)
    if results.problems:
        raise ValueError(f"{experiment}: " + "; ".join(results.problems))
    decisions = json.loads((experiment / "decisions.json").read_text())
    mock = decisions["mock"]

    built: dict[str, Scenario] = {}
    for vid, row in results.runs.items():
        if row["logs"]:
            _, scenario, problems = m1.build(m1.variant_from(row["variant"]), mock, written)
            if scenario is None:
                raise ValueError(f"{vid} cannot be rebuilt: " + "; ".join(problems.rejections))
            built[vid] = scenario

    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for vid, scenario in built.items():
        samples = results.of(vid)
        values = {name: results.scores([vid], name)[vid] for name in scorers(scenario)}
        for i, sample in enumerate(samples):
            log = read_eval_log(sample.log)
            for inspect_sample in log.samples or []:
                inspect_sample.epoch = sample.epoch
            target = out / scenario.name / sample.condition / Path(sample.log).name
            if target.exists():
                raise FileExistsError(f"{target} already exists: two logs share a name")
            target.parent.mkdir(parents=True, exist_ok=True)
            write_eval_log(log, str(target))
            rows.append({
                "scenario": scenario.name, "condition": sample.condition,
                "epoch": sample.epoch, "log": target.name, "seat": scenario.seat,
                "variant_id": vid, "base": sample.base, "error": sample.error,
                "agents": {agent: {name: values[name][agent][i] for name in values}
                           for agent in results.targets(vid)}})
    (out / "results.json").write_text(json.dumps({"results": rows}, indent=1) + "\n")

    conditions = sorted({s.variant["inner"]["condition"] for s in built.values()})
    epochs = max((row["epochs_done"] for row in results.runs.values()), default=0)
    manifest = build_manifest({s.name: s for s in built.values()}, conditions, epochs,
                              m1.models_for(mock), pr=pr, argv=list(argv))
    run = decisions["provenance"]
    if manifest["sha"] != run["commit"]:
        manifest["branch"] = None
    manifest.update(sha=run["commit"], short_sha=run["commit"] and run["commit"][:7],
                    dirty=run["dirty"], started_at=_iso(decisions["started"]),
                    finished_at=_iso(decisions.get("finished")),
                    run_config=decisions.get("run_config"),
                    without_run_config=decisions.get("without_run_config"))
    if audit is not None:
        manifest["study"] = {**study_of(audit), "experiment": experiment.resolve().name}
        if probes := probes_of(built):
            manifest["study"]["probes"] = probes
    write_manifest(out, manifest)

    if audit is not None:
        ran = sorted({s.base for s in built.values()} & set(written))
        (out / "report.html").write_text(report_html(audit, experiment, ran))
    return out


def probes_of(built: dict[str, Scenario]) -> dict[str, dict[str, dict[str, Any]]]:
    """Each variant's probes, by target agent: the question, its options and the answer
    that counts as adopting."""
    return {vid: {agent: {"question": probe.question, "options": list(probe.options),
                          "adopted": probe.adopted}
                  for agent, probe in scenario.probes.items()}
            for vid, scenario in built.items() if scenario.probes}


def _iso(stamp: str | None) -> str | None:
    """An ``experiment.py`` time stamp (``%Y-%m-%dT%H:%M:%S%z``) as ISO 8601."""
    return None if stamp is None else datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z").isoformat()


def report_html(audit: Path, experiment: Path, scenarios: Sequence[str]) -> str:
    """The audit's report, then its brief, the experiment's variants and the code of the
    written scenarios it ran (``scenarios``), as one page."""
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
    variants = [json.loads(line) for line in
                (experiment / "variants.jsonl").read_text().splitlines() if line.strip()]
    parts = [markdown.render((audit / "report.md").read_text()),
             "<h1>Brief</h1>", markdown.render((audit / "brief.md").read_text()),
             "<h1>Variants</h1>"]
    parts += [f"<pre>{html.escape(json.dumps(v, indent=2))}</pre>" for v in variants]
    parts.append("<h1>Written scenarios</h1>")
    for name in scenarios:
        code = (audit / "scenarios" / f"{name}.py").read_text()
        sha = (audit / "scenarios" / f"{name}.sha256").read_text().strip()
        parts += [f"<h2>{html.escape(name)}</h2>",
                  f"<p>sha256: <code>{html.escape(sha)}</code></p>",
                  f"<pre>{html.escape(code)}</pre>"]
    title = html.escape(f"{audit.name}: {experiment.name}")
    return ("<!doctype html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>{title}</title></head><body>\n" + "\n".join(parts) + "\n</body></html>\n")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("experiment", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pr", type=int, default=None,
                        help="PR to file the run under (default: $COSMOS_PR or $GITHUB_PR)")
    parser.add_argument("--name", default=None, help="the run's display name in Cosmos")
    args = parser.parse_args(argv)
    out = export(args.experiment, args.out, pr=args.pr,
                 argv=sys.argv[1:] if argv is None else argv)
    command = ["cosmos", "upload", str(out)] + (["--name", args.name] if args.name else [])
    print(shlex.join(command))


if __name__ == "__main__":
    main()
