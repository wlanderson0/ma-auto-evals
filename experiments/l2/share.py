"""Share an ended L2 audit on Cosmos in one step: export, upload, wait, link.

Usage::

    uv run python -m experiments.l2.share logs/l2-audit/<name> [--pr N]

or ``--share`` on ``experiments.l2.audit``, which runs it when the audit ends. For each
experiment of the audit it

1. exports it as a Cosmos run folder (``experiments.l2.cosmos``) in the audit's own
   ``cosmos/<exp>`` (written to a temporary folder first and moved into place when
   complete, so a folder there is always a whole export of this audit);
2. uploads it with the ``cosmos`` CLI (``cosmos upload <folder> --name "<audit> <exp>"``),
   which signs in as you, checks that the run Cosmos made is this experiment's (the
   manifest's commit and start time), and keeps its slug in the audit's
   ``cosmos-runs.json``, so running the command again never uploads it twice;
3. waits until Cosmos has parsed the runs (status ``ready``), 15 minutes at most;

then resolves the links (``experiments.l2.cosmos_links``) and writes the claims section
of ``report.md`` again with them. It prints each run's link and the report's path.
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

from experiments.l2.cosmos_links import Get, _same_run, http_get, resolve, sign_in

RUN_URL = re.compile(r"/runs/([A-Za-z0-9_-]{1,64})\s*$")
Upload = Callable[[Path, str, int | None], str]
"""``upload(folder, name, pr)``: the run's slug."""


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
          get: Get | None = None, base_url: str | None = None,
          sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print,
          ) -> dict[str, Any]:
    """Export, upload and link every experiment of the audit; the links."""
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
    if get is None or base_url is None:
        url, token = sign_in()
        get, base_url = get or http_get(url, token), base_url or url
    record = audit_dir / "cosmos-runs.json"
    runs: dict[str, str] = json.loads(record.read_text()) if record.exists() else {}
    for exp in experiments:
        if exp.name not in runs:
            folder = audit_dir / "cosmos" / exp.name
            if not (folder / "manifest.json").exists():
                _export(export, exp, folder, pr)
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
    wait_ready(get, [runs[exp.name] for exp in experiments], sleep=sleep)
    claims = audit_dir / "claims.json"
    tools.claims = json.loads(claims.read_text()) if claims.exists() else []
    try:
        links = resolve(tools, runs, get, base_url)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    (audit_dir / "cosmos.json").write_text(json.dumps(links, indent=2) + "\n")
    path = relink(audit_dir)
    say(f"report with Cosmos links: {path} ({len(links['samples'])} runs, "
        f"{len(links['posts'])} cited messages)")
    for problem in links["problems"]:
        say(f"not linked: {problem}")
    return links


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
