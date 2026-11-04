"""Cosmos links for an L2 audit's claims, resolved after its experiments are uploaded.

Usage::

    uv run python -m experiments.l2.cosmos_links <audit> --run exp-1=<slug> [--run ...]

Each ``--run`` names one of the audit's experiments and the slug of its Cosmos run (the
end of the link ``cosmos upload`` printed). Cosmos gives a sample its page, and a message
its place in the transcript, only when the run is parsed, so the links cannot be written
before the upload. This command asks Cosmos for them, with the same sign-in as the
``cosmos`` CLI (``COSMOS_URL`` and ``COSMOS_TOKEN``, else ``cosmos login``'s config),
saves them in the audit as ``cosmos.json``, and writes the claims section of the report
again with them (the rest of ``report.md`` is kept as it is):

- every sample of a variant a claim rests on: ``<server>/samples/<id>``, so a rate or a
  comparison links to each run behind its count, marked with what that run showed;
- every post a claim cites: ``<server>/samples/<id>?branch=<b>&kind=channel#msg-<seq>``,
  the message itself, matched by its message id (Cosmos's ``source_id``) on the sample's
  adopted branch.

Each slug is checked against its experiment: the run's manifest must name the commit and
start time in the experiment's ``decisions.json``, and its samples must be exactly the
experiment's (each variant's built name, condition and epoch), so links never point at
another audit's runs. A message that cannot be found, or a Cosmos error on one, is
recorded in ``cosmos.json``'s ``problems`` and left unlinked.

Only the claims section and ``cosmos.json`` are written; the record of the audit is not
changed. The report on Cosmos (its "Open report") is the one uploaded and has no links.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from experiments.l2.claims import find_post

Get = Callable[[str], Any]
"""``get(path)``: the JSON of a GET on the Cosmos API (``path`` starts with ``/api/``)."""
SLUG = re.compile(r"[A-Za-z0-9_-]{1,64}")
PAGE = 500


def resolve(tools: Any, runs: Mapping[str, str], get: Get, base_url: str) -> dict[str, Any]:
    """The links, from the audit's results (``tools``) and Cosmos. ``runs`` maps an
    experiment's name (``exp-1``) to its Cosmos run's slug. Raises ``ValueError`` when a
    slug is not its experiment's run."""
    base = base_url.rstrip("/")
    results, problems = tools.results(), []
    experiments = {p.name: p for p in tools.audit.experiments()}
    samples: dict[str, str] = {}
    sample_ids: dict[tuple[str, int], int] = {}
    for name, slug in runs.items():
        if not SLUG.fullmatch(slug):
            raise ValueError(f"{name}: {slug!r} is not a Cosmos run slug")
        if name not in experiments:
            raise ValueError(f"{name} is not an experiment of this audit")
        run = get(f"/api/runs/{slug}")
        decisions = json.loads((experiments[name] / "decisions.json").read_text())
        _same_run(name, slug, run.get("manifest") or {}, decisions)
        ours = {(f"{s.base}--{vid}", s.condition, s.epoch): (vid, s.epoch)
                for vid in results.runs if tools._where.get(vid) == name
                for s in results.of(vid)}
        theirs = {(str(i["scenario"]), str(i["condition"]), int(i["epoch"])): int(i["id"])
                  for i in run.get("samples") or []}
        if set(ours) != set(theirs):
            missing, extra = sorted(set(ours) - set(theirs)), sorted(set(theirs) - set(ours))
            raise ValueError(f"{slug} is not {name}'s run: its samples differ "
                             f"(missing {missing[:3]}, extra {extra[:3]})")
        for key, (vid, epoch) in ours.items():
            sample_ids[(vid, epoch)] = theirs[key]
            samples[f"{vid}/{epoch}"] = f"{base}/samples/{theirs[key]}"
    posts: dict[str, str] = {}
    refs = {(r["variant"], r["epoch"], r["post"]) for claim in tools.claims or []
            for r in claim["evidence"] if "call" not in r}
    for vid, epoch, post in sorted(refs, key=str):
        sid = sample_ids.get((vid, epoch))
        if sid is None:
            continue
        try:
            url = _message(tools, get, base, sid, vid, epoch, post)
        except Exception as error:  # one message, not the whole command
            problems.append(f"{vid} epoch {epoch} post {post}: {type(error).__name__}: {error}")
            continue
        if url is None:
            problems.append(f"{vid} epoch {epoch} post {post}: not found on Cosmos")
        else:
            posts[f"{vid}/{epoch}/{post}"] = url
    return {"base_url": base, "runs": {name: f"{base}/runs/{slug}" for name, slug in runs.items()},
            "samples": samples, "posts": posts, "problems": problems}


def _same_run(name: str, slug: str, manifest: Mapping[str, Any],
              decisions: Mapping[str, Any]) -> None:
    """The run's manifest names the experiment's commit and start time."""
    commit = (decisions.get("provenance") or {}).get("commit")
    if commit and manifest.get("sha") != commit:
        raise ValueError(f"{slug} is not {name}'s run: its commit is {manifest.get('sha')}, "
                         f"{name} ran at {commit}")
    started = decisions.get("started")
    if started and _when(manifest.get("started_at")) != _when(started):
        raise ValueError(f"{slug} is not {name}'s run: it started at "
                         f"{manifest.get('started_at')}, {name} at {started}")


def _when(stamp: Any) -> datetime | None:
    try:
        return datetime.strptime(str(stamp).replace(":", ""), "%Y-%m-%dT%H%M%S%z")
    except ValueError:
        return None


def _message(tools: Any, get: Get, base: str, sid: int, vid: str, epoch: int,
             post: Any) -> str | None:
    """A cited post's link: on the branch Cosmos shows by default (the last completed
    one, else the last), the message whose ``source_id`` is the post's message id."""
    message = find_post(tools, {"variant": vid, "epoch": epoch, "post": post})
    branches = get(f"/api/samples/{sid}").get("branches") or []
    done = [b for b in branches if b.get("status") == "completed"]
    branch = (done or branches or [None])[-1]
    if branch is None or not SLUG.fullmatch(str(branch.get("key"))):
        return None
    key, offset = str(branch["key"]), 0
    while True:
        page = get(f"/api/samples/{sid}/branches/{key}/messages?kind=channel&limit={PAGE}"
                   f"&offset={offset}")
        for m in page["items"]:
            if m.get("source_id") == message.get("id"):
                query = urlencode({"branch": key, "kind": "channel"})
                return f"{base}/samples/{sid}?{query}#msg-{int(m['seq'])}"
        offset += len(page["items"])
        if not page["items"] or offset >= int(page["total"]):
            return None


def http_get(base_url: str, token: str | None) -> Get:
    import httpx

    client = httpx.Client(base_url=base_url, timeout=60, headers={
        "Accept": "application/json", "User-Agent": "ma-auto-evals-l2",
        **({"Authorization": f"Bearer {token}"} if token else {})})

    def get(path: str) -> Any:
        response = client.get(path)
        if response.status_code == 401:
            raise SystemExit("Cosmos refused the sign-in (HTTP 401): run `cosmos login` again")
        response.raise_for_status()
        return response.json()
    return get


def sign_in() -> tuple[str, str | None]:
    """The Cosmos server and token, as the ``cosmos`` CLI finds them."""
    config: dict[str, Any] = {}
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(base) / "cosmos" / "config.json"
    if path.exists():
        config = json.loads(path.read_text())
    url = os.environ.get("COSMOS_URL") or config.get("url")
    if not url:
        raise SystemExit("no Cosmos server: set COSMOS_URL or run `cosmos login --url URL`")
    return url, os.environ.get("COSMOS_TOKEN") or config.get("token")


def main(argv: list[str] | None = None) -> None:
    from experiments.l2.audit import Audit
    from experiments.l2.audit_report import relink
    from experiments.l2.brief import parse_brief
    from experiments.l2.tools import Tools

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("audit", type=Path)
    parser.add_argument("--run", action="append", default=[], metavar="EXPERIMENT=SLUG",
                        help="an experiment and its Cosmos run's slug, e.g. exp-2=pd23mtf5")
    args = parser.parse_args(argv)
    if any("=" not in item for item in args.run):
        parser.error("each --run is exp-<n>=<slug>")
    runs = dict(item.split("=", 1) for item in args.run)
    if not runs:
        parser.error("give at least one --run exp-<n>=<slug>")
    tools = Tools(Audit(args.audit, parse_brief(args.audit / "brief.md")))
    if not tools.audit.experiments():
        parser.error(f"{args.audit} has no experiments of its own to link (an analysis-only "
                     "audit reads another audit's runs: link that audit)")
    claims = args.audit / "claims.json"
    tools.claims = json.loads(claims.read_text()) if claims.exists() else []
    url, token = sign_in()
    try:
        links = resolve(tools, runs, http_get(url, token), url)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    (args.audit / "cosmos.json").write_text(json.dumps(links, indent=2) + "\n")
    path = relink(args.audit)
    print(f"{len(links['samples'])} sample links and {len(links['posts'])} message links; "
          f"report: {path}")
    for problem in links["problems"]:
        print(f"not linked: {problem}")


if __name__ == "__main__":
    main()
