"""An L2 audit as a Cosmos study: the audit run, and the ``study`` its runs name.

Usage::

    uv run python -m experiments.l2.study <audit> --out <folder> [--pr N]

Cosmos groups an audit's runs as one study (Cosmos spec 12, §12.1 "Upload"). Every run of
the audit names it in its manifest's ``study``:

- ``id``: the audit's directory name and start time, ``<name>@<started>``, so the same
  audit always has the same id and two audits never share one. The start is that of L2's
  own log (``l2/*.eval``); an audit without one (its tools driven directly, as in the
  tests) starts when its first experiment did.
- ``name``: the audit's directory name, which Cosmos shows.
- In an experiment's run (``experiments.l2.cosmos``): ``experiment``, its ``exp-<n>``.
- In the audit run: ``role: "audit"``, and ``runs``, the Cosmos slug of each experiment
  already shared (``{"exp-<n>": "<slug>"}``, from ``cosmos-runs.json``), so Cosmos can join
  experiments uploaded before their manifests named the study.

The **audit run** is one more Cosmos run beside the experiments'. Its folder holds

- ``manifest.json``: the commit and dirty flag the experiments ran at (from the first
  one's ``decisions.json``, as ``experiments.l2.cosmos`` does), the PR, the start and end
  of L2's log, ``models: {"l2": <L2's model>}`` and the study;
- ``l2/<log>.eval``: L2's own Inspect log, unchanged;
- under ``study/``: ``claims.json`` (when L2 ended the audit), ``ledger.json``,
  ``brief.json`` (``{"text": <brief.md as given>}``), and ``decisions.json``,
  ``proposed.json`` and ``launches.json``, the audit's JSONL files as JSON arrays (empty
  when the audit has none): ``cosmos upload`` sends only ``.eval``, ``.json`` and
  ``.html`` files. ``review.json`` too, copied as it is, when the audit has one.

Cosmos servers older than studies keep the ``study`` key as part of the manifest and
file ``study/*.json`` as plain files, so they accept the upload as before.

**Annotations.** ``annotations.jsonl`` (L2 spec §6.2) holds comments and tags in
Cosmos's record shape (Cosmos spec 13, "Annotations import"): ``{"id", "kind": "comment"
| "tag", "target": {"type": "run" | "sample" | "branch" | "message", "variant", "epoch",
"branch", "seq"}, "body", "tags", "author", "at"}``. It sits next to an experiment's logs
(``experiments/exp-<n>/annotations.jsonl``), and its records go to that experiment's run.
An ``annotations.jsonl`` in the audit's own directory is read too: each record goes to the
experiment that ran its ``variant`` (or its ``scenario``, ``<base>--<variant>``), else,
for a record naming neither, to the audit run. ``annotation_batches`` sorts them;
``experiments.l2.share`` posts them after the upload.
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from inspect_ai.log import read_eval_log

AUDIT_RUN = "audit"
"""The audit run's name in ``cosmos-runs.json`` and its folder in ``cosmos/``."""
JSONL = ("decisions", "proposed", "launches")
"""The audit's JSONL files the audit run carries as JSON arrays."""


def l2_logs(audit: Path) -> list[Path]:
    """L2's own Inspect logs in the audit, oldest first (one, for an audit L2 ran)."""
    return sorted((Path(audit) / "l2").glob("*.eval"))


def _l2_header(audit: Path) -> Any | None:
    logs = l2_logs(audit)
    return read_eval_log(str(logs[0]), header_only=True) if logs else None


def _experiments(audit: Path) -> list[Path]:
    found = [p for p in (Path(audit) / "experiments").glob("exp-*")
             if p.is_dir() and p.name[4:].isdigit()]
    return sorted(found, key=lambda p: int(p.name[4:]))


def _decisions(experiment: Path) -> dict[str, Any]:
    return json.loads((experiment / "decisions.json").read_text())


def times(audit: Path) -> tuple[str | None, str | None]:
    """When the audit started and ended: its L2 log's, else its experiments'."""
    from experiments.l2.cosmos import _iso

    header = _l2_header(audit)
    if header is not None and header.stats.started_at:
        return header.stats.started_at, header.stats.completed_at or None
    experiments = _experiments(audit)
    if not experiments:
        return None, None
    return (_iso(_decisions(experiments[0]).get("started")),
            _iso(_decisions(experiments[-1]).get("finished")))


def study_of(audit: Path) -> dict[str, str]:
    """The study every run of ``audit`` names: ``{id, name}``."""
    audit = Path(audit).resolve()
    started, _ = times(audit)
    if started is None:
        raise ValueError(f"{audit} has no L2 log and no experiment to date it by")
    return {"id": f"{audit.name}@{started}", "name": audit.name}


def jsonl(path: Path) -> list[Any]:
    """A JSONL file's records (none when it does not exist)."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def audit_study(audit: Path, runs: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The audit run's ``study``: ``role: "audit"`` and the slugs of the experiments in
    ``runs`` (``cosmos-runs.json``'s map), when there are any."""
    shared = {name: slug for name, slug in (runs or {}).items() if name != AUDIT_RUN}
    return {**study_of(audit), "role": "audit", **({"runs": shared} if shared else {})}


def annotation_files(audit: Path) -> list[Path]:
    """The audit's ``annotations.jsonl`` files: its own, then each experiment's."""
    found = [Path(audit) / "annotations.jsonl"]
    found += [exp / "annotations.jsonl" for exp in _experiments(audit)]
    return [path for path in found if path.is_file()]


def export_audit(audit: Path, out: Path, *, pr: int | None = None,
                 runs: Mapping[str, str] | None = None) -> Path:
    """Write the audit run's folder for ``audit`` to ``out`` (new or empty); return ``out``.
    ``runs``: the experiments already on Cosmos, by name (``cosmos-runs.json``)."""
    from experiments.run import REPO, git_info, resolve_pr, write_manifest

    audit, out = Path(audit), Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty")
    experiments = _experiments(audit)
    if not experiments:
        raise ValueError(f"{audit} has no experiments of its own: there is no study to share")
    started, finished = times(audit)
    header = _l2_header(audit)
    run = _decisions(experiments[0])["provenance"]
    manifest: dict[str, Any] = {"schema": "cosmos.manifest/1", **git_info(REPO),
                                "pr": resolve_pr(pr)}
    if manifest["sha"] != run["commit"]:
        manifest["branch"] = None
    manifest.update(sha=run["commit"], short_sha=run["commit"] and run["commit"][:7],
                    dirty=run["dirty"], started_at=started, finished_at=finished,
                    models={"l2": str(header.eval.model)} if header is not None else {},
                    study=audit_study(audit, runs))
    (out / "l2").mkdir(parents=True, exist_ok=True)
    for log in l2_logs(audit):
        shutil.copyfile(log, out / "l2" / log.name)
    files: dict[str, Any] = {
        "ledger": json.loads((audit / "ledger.json").read_text()),
        "brief": {"text": (audit / "brief.md").read_text()},
        **{name: jsonl(audit / f"{name}.jsonl") for name in JSONL}}
    if (audit / "claims.json").exists():  # absent until L2 ends the audit
        files["claims"] = json.loads((audit / "claims.json").read_text())
    (out / "study").mkdir()
    for name, data in files.items():
        (out / "study" / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
    if (audit / "review.json").exists():
        shutil.copyfile(audit / "review.json", out / "study" / "review.json")
    write_manifest(out, manifest)
    return out


def annotation_batches(audit: Path, where: Mapping[str, str]) -> tuple[
        dict[str, list[dict[str, Any]]], list[str]]:
    """The records of the audit's ``annotations.jsonl`` files by the run they go to (an
    experiment's name, or ``AUDIT_RUN``), and the records that go nowhere, with why.
    ``where`` maps each variant id the audit ran to its experiment."""
    audit = Path(audit)
    batches: dict[str, list[dict[str, Any]]] = {}
    problems: list[str] = []
    for path in annotation_files(audit):
        experiment = None if path.parent == audit else path.parent.name
        name = path.relative_to(audit).as_posix()
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                problems.append(f"{name} line {n}: not JSON ({error})")
                continue
            if not isinstance(record, dict):
                problems.append(f"{name} line {n}: not a record")
            elif experiment is not None:  # next to its experiment's logs: that run's
                batches.setdefault(experiment, []).append(record)
            else:
                _route(record, where, batches, problems, f"{name} line {n}")
    return batches, problems


def _route(record: dict[str, Any], where: Mapping[str, str],
           batches: dict[str, list[dict[str, Any]]], problems: list[str], line: str) -> None:
    """An audit-wide record to the experiment that ran its variant, else the audit run."""
    target = record.get("target")
    if not isinstance(target, dict):
        problems.append(f"{line}: no target")
        return
    variant = target.get("variant")
    if variant is None and isinstance(target.get("scenario"), str):
        _, dashes, rest = target["scenario"].rpartition("--")
        variant = rest if dashes else None
    if variant is None and target.get("scenario") is None:
        batches.setdefault(AUDIT_RUN, []).append(record)
    elif variant in where:
        batches.setdefault(where[variant], []).append(record)
    else:
        problems.append(f"{line} ({record.get('id')}): no experiment of this audit ran "
                        f"{variant or target.get('scenario')!r}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("audit", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pr", type=int, default=None, help="PR to file the run under")
    args = parser.parse_args(argv)
    record = args.audit / "cosmos-runs.json"  # the experiments already on Cosmos
    runs = json.loads(record.read_text()) if record.exists() else None
    out = export_audit(args.audit, args.out, pr=args.pr, runs=runs)
    print(f"cosmos upload {out}")


if __name__ == "__main__":
    main()
