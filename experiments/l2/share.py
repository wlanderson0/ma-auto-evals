"""Share an ended L2 audit on Cosmos in one step: export, upload, wait, link.

Usage::

    uv run python -m experiments.l2.share logs/l2-audit/<name> [--pr N]

or ``--share`` on ``experiments.l2.audit``, which runs it when the audit ends. For each
experiment of the audit it

1. exports it as a Cosmos run folder (``experiments.l2.cosmos``, whose manifest names the
   audit's study) in the audit's own ``cosmos/<exp>`` (written to a temporary folder
   first and moved into place when complete, so a folder there is always a whole export
   of this audit);
2. uploads it with the ``cosmos`` CLI (``cosmos upload <folder> --name "<audit> <exp>"``),
   which signs in as you, checks that the run Cosmos made is this experiment's (the
   manifest's commit and start time), and keeps its slug in the audit's
   ``cosmos-runs.json``, so running the command again never uploads it twice.

Then it does the same for the audit run (``experiments.l2.study``: L2's own log, its
claims, ledger, brief and records), in ``cosmos/audit``, uploaded as ``"<audit> audit"``
and kept in ``cosmos-runs.json`` as ``audit``; the run Cosmos made must name the audit's
study. Before that upload it asks the CLI (``--dry-run``) which files it would skip, and
stops if they include the write-up or an image: a CLI older than Cosmos's reports leaves
``.md`` and image files out without a word. Its manifest lists the slug of every
experiment already on Cosmos (``study.runs``), so an audit whose experiments were shared
before manifests named a study becomes one when shared again: only the audit run is uploaded.

It waits until Cosmos has parsed every run (status ``ready``), 15 minutes at most;
resolves the links (``experiments.l2.cosmos_links``) and writes the claims section of
``report.md`` again with them; then posts the audit's ``annotations.jsonl`` files, if it
has any (each experiment's, and the audit's own), to the runs that hold their targets
(``POST /api/runs/{slug}/annotations``: Cosmos never imports a record twice, so posting
again changes nothing). A failed post is a warning naming the run, never an error. It
prints each run's link and the report's path.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from experiments.l2.cosmos_links import (
    Get,
    NotOnServer,
    Post,
    _same_run,
    http_get,
    http_post,
    resolve,
    sign_in,
)
from experiments.l2.study import (
    AUDIT_RUN,
    annotation_batches,
    annotation_files,
    audit_study,
    export_audit,
    study_of,
)

RUN_URL = re.compile(r"/runs/([A-Za-z0-9_-]{1,64})\s*$")
Upload = Callable[[Path, str, int | None], str]
"""``upload(folder, name, pr)``: the run's slug."""
BATCH = 5000
"""Annotation records per request (the most Cosmos takes at once)."""


def cosmos_upload(folder: Path, name: str, pr: int | None) -> str:
    """Upload ``folder`` with the ``cosmos`` CLI; the run's slug (from the link it prints)."""
    command = ["cosmos", "upload", str(folder), "--name", name,
               *(["--pr", str(pr)] if pr is not None else [])]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired as error:
        raise SystemExit(f"cosmos upload {folder} took over 30 minutes; check Cosmos for a "
                         "run before sharing again") from error
    except FileNotFoundError as error:
        raise SystemExit("the cosmos CLI is not installed (docs/cosmos.md, "
                         "\"First time\")") from error
    if done.returncode != 0:
        raise SystemExit(f"cosmos upload {folder} failed:\n{done.stderr.strip()[-2000:]}")
    lines = done.stdout.strip().splitlines()
    found = RUN_URL.search(lines[-1]) if lines else None
    if found is None:
        raise SystemExit(f"cosmos upload {folder} printed no run link: {done.stdout[-500:]!r}")
    return found.group(1)


DOCUMENTS = (".md", ".png", ".jpg", ".jpeg", ".webp")
"""The audit run's documents (the write-up and its images): a ``cosmos`` CLI older than
Cosmos's reports skips them without a word."""


def cosmos_skipped(folder: Path) -> list[str] | None:
    """The files ``cosmos upload <folder>`` would skip (``--dry-run``: nothing is sent and no
    token is needed); ``None`` when the dry run cannot say."""
    try:
        done = subprocess.run(["cosmos", "upload", str(folder), "--dry-run"],
                              capture_output=True, text=True, timeout=300)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if done.returncode != 0:
        return None
    return [line.split(None, 1)[1].strip() for line in done.stdout.splitlines()
            if line.startswith("  skipped ") and len(line.split(None, 1)) == 2]


def check_documents(folder: Path, skipped: Callable[[Path], list[str] | None],
                    say: Callable[[str], None]) -> None:
    """Stop before uploading the audit run when the CLI would leave out its write-up or
    images, so Cosmos never shows a study without them."""
    found = skipped(folder)
    if found is None:
        say(f"could not check which files cosmos uploads from {folder}; if the study shows no "
            "write-up, upgrade the cosmos CLI (uv tool upgrade cosmos-evals) and share again")
        return
    left = [path for path in found if path.lower().endswith(DOCUMENTS)]
    if left:
        named = ", ".join(left[:3]) + (f" and {len(left) - 3} more" if len(left) > 3 else "")
        raise SystemExit(f"your cosmos CLI would leave out {named}: it is older than Cosmos's "
                         "reports. Upgrade it (uv tool upgrade cosmos-evals) and share again; "
                         "the audit run was not uploaded")


def wait_ready(get: Get, slugs: list[str], timeout: float = 900, every: float = 5,
               sleep: Callable[[float], None] = time.sleep) -> None:
    """Until Cosmos has parsed every run in ``slugs``; raises if one fails or the wait
    (shared by all of them) runs out."""
    waited, left = 0.0, list(slugs)
    while left:
        for slug in list(left):
            status = get(f"/api/runs/{slug}").get("status")
            if status == "ready":
                left.remove(slug)
            elif status == "failed":
                raise SystemExit(f"Cosmos could not parse run {slug}: see its page")
        if not left:
            return
        if waited >= timeout:
            raise SystemExit(f"runs {left} are not parsed after {timeout:.0f} s; run this "
                             "command again later (nothing is uploaded twice)")
        sleep(every)
        waited += every


def share(audit_dir: Path, *, pr: int | None = None, upload: Upload = cosmos_upload,
          get: Get | None = None, base_url: str | None = None, post: Post | None = None,
          sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print,
          skipped: Callable[[Path], list[str] | None] | None = None,
          ) -> dict[str, Any]:
    """Export, upload and link every experiment of the audit, and its audit run; the
    links. ``skipped`` says which files of the audit run the upload would leave out
    (``cosmos_skipped`` with the real upload, else no check)."""
    from experiments.l2.audit import Audit
    from experiments.l2.audit_report import relink
    from experiments.l2.brief import parse_brief
    from experiments.l2.cosmos import export
    from experiments.l2.tools import Tools

    audit_dir = Path(audit_dir)
    tools = Tools(Audit(audit_dir, parse_brief(audit_dir / "brief.md")))
    experiments = tools.audit.experiments()
    if not experiments:
        raise SystemExit(f"{audit_dir} has no experiments of its own to share")
    annotated = bool(annotation_files(audit_dir))
    if get is None or base_url is None or (post is None and annotated):
        url, token = sign_in()
        get, base_url = get or http_get(url, token), base_url or url
        post = post or http_post(url, token)
    study = study_of(audit_dir)
    record = audit_dir / "cosmos-runs.json"
    runs: dict[str, str] = json.loads(record.read_text()) if record.exists() else {}
    for exp in experiments:
        if exp.name not in runs:
            folder = audit_dir / "cosmos" / exp.name
            if not (folder / "manifest.json").exists():
                _export(export, exp, folder, pr)
            _name_study(folder, {**study, "experiment": exp.name})
            slug = upload(folder, f"{audit_dir.name} {exp.name}", pr)
            run = get(f"/api/runs/{slug}")
            decisions = json.loads((exp / "decisions.json").read_text())
            try:
                _same_run(exp.name, slug, run.get("manifest") or {}, decisions)
            except ValueError as error:  # not recorded, so a rerun uploads afresh
                raise SystemExit(f"{error}; it was not recorded") from error
            runs[exp.name] = slug
            record.write_text(json.dumps(runs, indent=2) + "\n")  # kept at once: never twice
        say(f"{exp.name}: {base_url.rstrip('/')}/runs/{runs[exp.name]}")
    if AUDIT_RUN not in runs:
        folder = audit_dir / "cosmos" / AUDIT_RUN
        if not (folder / "manifest.json").exists():
            _export(export_audit, audit_dir, folder, pr)
        _name_study(folder, audit_study(audit_dir, runs))
        if skipped is None and upload is cosmos_upload:
            skipped = cosmos_skipped
        if skipped is not None:
            check_documents(folder, skipped, say)
        slug = upload(folder, f"{audit_dir.name} {AUDIT_RUN}", pr)
        named = (get(f"/api/runs/{slug}").get("manifest") or {}).get("study") or {}
        if named.get("id") != study["id"] or named.get("role") != "audit":
            raise SystemExit(f"{slug} is not this audit's run (its study is {named or None}, "
                             f"this audit's is {study['id']}); it was not recorded")
        runs[AUDIT_RUN] = slug
        record.write_text(json.dumps(runs, indent=2) + "\n")
    say(f"{AUDIT_RUN}: {base_url.rstrip('/')}/runs/{runs[AUDIT_RUN]}")
    wait_ready(get, [runs[exp.name] for exp in experiments] + [runs[AUDIT_RUN]], sleep=sleep)
    claims = audit_dir / "claims.json"
    tools.claims = json.loads(claims.read_text()) if claims.exists() else []
    try:
        links = resolve(tools, {name: slug for name, slug in runs.items() if name != AUDIT_RUN},
                        get, base_url)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    (audit_dir / "cosmos.json").write_text(json.dumps(links, indent=2) + "\n")
    path = relink(audit_dir)
    say(f"report with Cosmos links: {path} ({len(links['samples'])} runs, "
        f"{len(links['posts'])} cited messages)")
    for problem in links["problems"]:
        say(f"not linked: {problem}")
    if annotated:  # after the links, so an import that fails never costs them
        assert post is not None
        tools.results()  # which experiment ran each variant
        post_annotations(audit_dir, runs, dict(tools._where), post, say)
    return links


def post_annotations(audit_dir: Path, runs: dict[str, str], where: dict[str, str], post: Post,
                     say: Callable[[str], None] = print) -> dict[str, int]:
    """Post the audit's ``annotations.jsonl`` files to the runs that hold their targets; the
    counts Cosmos gave back. Every failure is a warning naming the run, never an error: a
    server without the import (HTTP 404 or 405) stops the posting; any other (an HTTP
    error, a timeout) skips that batch, which the next share posts again."""
    batches, problems = annotation_batches(audit_dir, where)
    total = {"created": 0, "unchanged": 0, "problems": len(problems)}
    for problem in problems:
        say(f"annotation not posted: {problem}")
    for name, records in batches.items():
        for start in range(0, len(records), BATCH):
            try:
                answer = post(f"/api/runs/{runs[name]}/annotations",
                              {"records": records[start:start + BATCH]})
            except NotOnServer as error:
                say(f"annotations not posted: this Cosmos cannot import them ({error}); "
                    "the runs are shared without them")
                return total
            except Exception as error:  # a warning: the runs and their links are shared
                total["failed"] = total.get("failed", 0) + 1
                say(f"annotations for {name} ({runs[name]}) not posted: {_why(error)}; "
                    "share again to post them")
                continue
            total["created"] += int(answer.get("created") or 0)
            total["unchanged"] += int(answer.get("unchanged") or 0)
            for problem in answer.get("problems") or []:
                total["problems"] += 1
                say(f"annotation {problem.get('id')} not imported on {name}: "
                    f"{problem.get('reason')}")
    say(f"annotations: {total['created']} created, {total['unchanged']} unchanged, "
        f"{total['problems']} not imported"
        + (f", {total['failed']} batches not posted" if total.get("failed") else ""))
    return total


def _why(error: Exception) -> str:
    """What went wrong, from its type and HTTP status only (a response body or a message
    could carry what the request sent)."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    return f"HTTP {status}" if status is not None else type(error).__name__


def _name_study(folder: Path, study: dict[str, Any]) -> None:
    """The export's manifest names ``study`` (an export made before studies did not, and
    an audit run's lists the experiments on Cosmos when it is uploaded)."""
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text())
    named = {**(manifest.get("study") or {}), **study}  # what the export added is kept
    if manifest.get("study") != named:
        manifest["study"] = named
        path.write_text(json.dumps(manifest, indent=2) + "\n")


def _export(export: Callable[..., Any], exp: Path, folder: Path, pr: int | None) -> None:
    """Export into a fresh temporary folder next to ``folder``, then move it into place:
    an export that fails leaves nothing behind to be reused."""
    folder.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder.parent, prefix=f".{folder.name}-") as tmp:
        export(exp, Path(tmp) / "run", pr=pr)
        if folder.exists():
            shutil.rmtree(folder)  # an earlier export that never completed
        (Path(tmp) / "run").rename(folder)


def share_or_explain(audit_dir: Path, **options: Any) -> bool:
    """``share``, with any failure (Cosmos unreachable, an HTTP error, ...) turned into a
    message saying how to retry; whether it worked."""
    try:
        share(audit_dir, **options)
    except (Exception, SystemExit) as error:  # a message saying how to retry, no traceback
        why = str(error) if isinstance(error, SystemExit) else (
            f"{type(error).__name__}: {error}")
        print(f"not shared on Cosmos: {why}\nshare it later with "
              f"`uv run python -m experiments.l2.share {audit_dir}` (nothing is uploaded twice)")
        return False
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("audit", type=Path)
    parser.add_argument("--pr", type=int, default=None, help="file the runs under this PR")
    args = parser.parse_args(argv)
    if not share_or_explain(args.audit, pr=args.pr):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
