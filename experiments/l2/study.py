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
  when the audit has none), so Cosmos reads them by name. ``review.json`` too, copied as
  it is, when the audit has one, and, when the audit was steered, ``steer.json`` (its
  ``steer.jsonl``, every request read from the mailbox, applied or refused) and
  ``report.json`` (``{"steered": n}``);
- L2's outputs as documents (Cosmos spec 16 §4.6), each only when the audit has it:
  ``writeup.md``, byte for byte; the images: every ``.png``, ``.jpg``, ``.jpeg`` and
  ``.webp`` directly under ``figures/``, and any other the write-up embeds by a path
  inside the audit directory, at the same relative path, never one that leads out of it
  (at most 400, the write-up's own first; one over 10 MB is left out with a note);
  ``report.html``, the audit's ``report.md`` as a page;
  ``study/writeup.json`` (``{"final", "body_line", "facts": [{"name", "value"}],
  "embeds", "missing"}``: whether the write-up is final, the line of ``writeup.md`` where
  L2's own text starts, the harness's facts above it, the images it embeds and those that
  are not files); and ``study/figures.json``, one entry per comparison figure, from the
  ``.json`` ``Tools._figure`` writes beside it (``{"path", "a", "b", "scorer", "field",
  "rounds", "measure", "call", "targets": {"<agent>": {"a": {"k", "n", "incomplete"},
  "b": {...}}}}``, ``call`` the first ``scores`` call that draws it). Those sidecars are
  not sent: a ``.json`` outside ``study/`` would be read as results. A part that cannot
  be made is left out with a note, never failing the export.

Cosmos servers older than studies keep the ``study`` key as part of the manifest and
file ``study/*.json`` as plain files, so they accept the upload as before; a ``cosmos``
CLI older than reports skips the ``.md`` and image files.

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
import posixpath
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from inspect_ai.log import read_eval_log

AUDIT_RUN = "audit"
"""The audit run's name in ``cosmos-runs.json`` and its folder in ``cosmos/``."""
JSONL = ("decisions", "proposed", "launches")
"""The audit's JSONL files the audit run carries as JSON arrays."""
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
"""The images the audit run carries (those Cosmos takes)."""
MAX_IMAGES = 400
MAX_IMAGE_BYTES = 10 * 2**20
"""Cosmos's limits on a run's images: an image past them is left out with a note."""


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
    if (audit / "steer.jsonl").exists():  # only a steered audit has these
        files["steer"] = jsonl(audit / "steer.jsonl")
    if (audit / "report.json").exists():
        files["report"] = json.loads((audit / "report.json").read_text())
    (out / "study").mkdir()
    for name, data in files.items():
        (out / "study" / f"{name}.json").write_text(json.dumps(data, indent=2) + "\n")
    if (audit / "review.json").exists():
        shutil.copyfile(audit / "review.json", out / "study" / "review.json")
    export_documents(audit, out)
    write_manifest(out, manifest)
    return out


def export_documents(audit: Path, out: Path, say: Callable[[str], None] = print) -> None:
    """The audit's write-up, its images, its report as a page, and ``study/writeup.json``
    and ``study/figures.json`` (Cosmos spec 16 §4.6), into the audit run's folder ``out``.
    A part that cannot be made is left out with a note: it never fails the export."""
    from experiments.l2.cosmos import audit_report_html

    audit, out = Path(audit), Path(out)
    parts: list[tuple[str, Callable[[], None]]] = [
        ("writeup.md", lambda: _copy_writeup(audit, out)),
        ("images", lambda: _copy_images(audit, out, say)),
        ("report.html", lambda: (out / "report.html").write_text(audit_report_html(audit))
         if _inside(audit, "report.md") else None),
        ("study/writeup.json", lambda: _write_json(out / "study" / "writeup.json",
                                                   writeup_json(audit))),
        ("study/figures.json", lambda: _write_json(out / "study" / "figures.json",
                                                   figures_json(audit, out, say))),
    ]
    for name, make in parts:
        try:
            make()
        except Exception as error:  # a document never costs the audit its upload
            say(f"{name} left out of the audit run: {type(error).__name__}: {error}")


def _write_json(path: Path, data: Any) -> None:
    if data:  # nothing to say: the file is absent
        path.write_text(json.dumps(data, indent=2) + "\n")


def _copy_writeup(audit: Path, out: Path) -> None:
    if _inside(audit, "writeup.md"):
        shutil.copyfile(audit / "writeup.md", out / "writeup.md")  # byte for byte


def images(audit: Path) -> list[str]:
    """The images the audit run carries, by their path relative to the audit: every image
    the write-up embeds by a path inside the audit directory, in its order (so its links
    resolve unchanged and come first under the limit), then every other image directly
    under ``figures/``. Only a regular file inside the audit directory is carried: a path
    or a link that leads out of it never is."""
    from experiments.l2.figures import FIGURES
    from experiments.l2.tools import embeds

    audit = Path(audit)
    found = embeds((audit / "writeup.md").read_text() if _inside(audit, "writeup.md") else None)
    found += sorted(f"{FIGURES}/{p.name}" for p in (audit / FIGURES).glob("*"))
    carried: list[str] = []
    for src in found:
        rel = _inside(audit, src)
        if rel is not None and Path(rel).suffix.lower() in IMAGE_SUFFIXES and rel not in carried:
            carried.append(rel)
    return carried


def _inside(audit: Path, src: str) -> str | None:
    """``src`` as a normalised path relative to the audit directory, when it names a
    regular file inside it (the links on its way resolved); ``None`` otherwise."""
    if Path(src).is_absolute() or "://" in src or "\\" in src:
        return None
    rel = posixpath.normpath(src)
    if rel in (".", "..") or rel.startswith(("../", "/")):
        return None
    root = audit.resolve()
    path = (root / rel).resolve()
    return rel if path.is_relative_to(root) and path.is_file() else None


def _copy_images(audit: Path, out: Path, say: Callable[[str], None]) -> None:
    sent, over = 0, []
    for rel in images(audit):
        try:
            source = (audit / rel).resolve()
            if source.stat().st_size > MAX_IMAGE_BYTES:
                say(f"{rel} left out of the audit run: an image over "
                    f"{MAX_IMAGE_BYTES // 2**20} MB")
                continue
            if sent == MAX_IMAGES:
                over.append(rel)
                continue
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, out / rel)
            sent += 1
        except OSError as error:  # one image that cannot be read costs only itself
            say(f"{rel} left out of the audit run: {type(error).__name__}: {error}")
    if over:
        say(f"{len(over)} images left out of the audit run: at most {MAX_IMAGES} are sent "
            f"({', '.join(over[:5])}{', ...' if len(over) > 5 else ''})")


def writeup_json(audit: Path) -> dict[str, Any] | None:
    """``study/writeup.json``: the parts ``write_writeup`` kept in the audit's
    ``writeup.json`` (read from ``writeup.md`` for an audit written before it kept them),
    the images the write-up embeds and those that are not files (``missing``)."""
    from experiments.l2.tools import embedded, embeds

    audit = Path(audit)
    if not _inside(audit, "writeup.md"):
        return None
    text = (audit / "writeup.md").read_text()
    kept = audit / "writeup.json"
    parts = json.loads(kept.read_text()) if kept.is_file() else _parts_of(text)
    found = list(dict.fromkeys(embeds(text)))
    return {"final": bool(parts["final"]), "body_line": int(parts["body_line"]),
            "facts": parts["facts"], "embeds": found,
            "missing": [src for src in found if embedded(audit, src) is None]}


def _parts_of(text: str) -> dict[str, Any]:
    """The parts of a ``writeup.md`` as ``write_writeup`` wrote it: its facts table, the
    draft marker, and the line after the ``---`` that ends the harness's part."""
    lines = text.split("\n")
    end = lines.index("---") if "---" in lines else -2
    facts = []
    for line in lines[:max(end, 0)]:
        cells = re.fullmatch(r"\| (.+?) \| (.*) \|", line)
        if cells:  # a fact's row; the table's empty header and rule have no cells
            facts.append({"name": cells.group(1),
                          "value": cells.group(2).replace("\\|", "|")})
    return {"final": not any(line.startswith("> **Draft:**") for line in lines[:max(end, 0)]),
            "body_line": end + 3, "facts": facts}


def figures_json(audit: Path, out: Path,
                 say: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """``study/figures.json``: one entry per comparison figure the audit run carries, from
    the sidecar ``Tools._figure`` wrote beside it, with ``call``, the trace's first
    successful ``scores`` call that draws it (``None`` if none is found). A sidecar that
    cannot be read is left out with a note."""
    from experiments.l2.figures import FIGURES

    audit = Path(audit)
    calls = figure_calls(audit)
    entries = []
    for sidecar in sorted((audit / FIGURES).glob("*.json")):
        path = sidecar.with_suffix(".png").relative_to(audit).as_posix()
        if not (Path(out) / path).is_file():
            continue  # the image was left out, or never drawn: a JSON of L2's own
        try:
            drawn = json.loads(sidecar.read_text())
            entries.append({
                "path": path, **{key: drawn.get(key) for key in
                                 ("a", "b", "scorer", "field", "rounds", "measure")},
                "call": calls.get(path),
                "targets": {agent: {side: {key: row[side][key] for key in
                                           ("k", "n", "incomplete")} for side in ("a", "b")}
                            for agent, row in drawn["compared"].items()}})
        except Exception as error:  # one sidecar costs only its own entry
            say(f"{path} left out of study/figures.json: {type(error).__name__}: {error}")
    return entries


def figure_calls(audit: Path) -> dict[str, int]:
    """Each comparison figure's path, by the ``seq`` of the first successful ``scores``
    call (L2's, not the reviewer's) whose ``variant_ids`` and ``against`` draw it."""
    from experiments.l2.figures import figure_name
    from experiments.l2.tools import _ids, _jsonl

    calls: dict[str, int] = {}
    rows = [row for row in _jsonl(Path(audit) / "trace.jsonl") if isinstance(row, dict)
            and isinstance(row.get("seq"), int) and isinstance(row.get("args"), dict)]
    for row in sorted(rows, key=lambda row: row["seq"]):
        args = row["args"]
        if (row.get("tool") != "scores" or not row.get("ok") or row.get("by") == "reviewer"
                or not args.get("against")):
            continue
        try:
            ids = _ids(args["variant_ids"])
            names = [figure_name(a, args["against"], args.get("scorer"), args.get("field"),
                                 args.get("rounds")) for a in ids if a != args["against"]]
        except Exception:  # a row this export cannot read draws nothing
            continue
        for name in names:
            calls.setdefault(name, row["seq"])
    return calls


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
