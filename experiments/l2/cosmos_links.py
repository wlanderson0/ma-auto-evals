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
  the message itself, matched by its message id (Cosmos's ``source_id``) on the branch
  Cosmos shows;
- every message a count was made from: for a claim read from ``counts`` (a target's uses
  of the scenario's name in its own words), each run's channel messages are looked up, so
  the report can link every post that counted (``messages``: per run, the branch and each
  message id's place on Cosmos).

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
    refs = {(r["variant"], r["epoch"], r["post"]) for claim in tools.claims or []
            for r in claim["evidence"] if "call" not in r}
    counted: dict[str, set[str]] = {}  # variant -> the targets whose counted posts are linked
    for claim in tools.claims or []:
        for vid in _counted_variants(tools, claim):
            counted.setdefault(vid, set()).add(claim["target"])
    cited = {(vid, epoch): {str(find_post(tools, {"variant": vid, "epoch": epoch, "post": post})
                                .get("sender"))}
             for vid, epoch, post in refs if _found(tools, vid, epoch, post)}
    wanted = sorted({*cited, *((vid, s.epoch) for vid in counted for s in results.of(vid))},
                    key=str)
    messages: dict[str, dict[str, Any]] = {}
    for vid, epoch in wanted:
        sid = sample_ids.get((vid, epoch))
        if sid is None:
            continue
        senders = counted.get(vid, set()) | cited.get((vid, epoch), set())
        try:
            found = _channel(get, sid, senders)
        except Exception as error:  # one run, not the whole command
            problems.append(f"{vid} epoch {epoch}: messages not read: "
                            f"{type(error).__name__}: {error}")
            continue
        if found is None:
            problems.append(f"{vid} epoch {epoch}: no branch to read messages from")
            continue
        key, seqs = found
        messages[f"{vid}/{epoch}"] = {"url": f"{base}/samples/{sid}?"
                                      + urlencode({"branch": key, "kind": "channel"}),
                                      "seq": seqs}
    posts: dict[str, str] = {}
    for vid, epoch, post in sorted(refs, key=str):
        run = messages.get(f"{vid}/{epoch}")
        if run is None:
            continue
        try:
            message = find_post(tools, {"variant": vid, "epoch": epoch, "post": post})
        except LookupError as error:
            problems.append(f"{vid} epoch {epoch} post {post}: {error}")
            continue
        seq = run["seq"].get(str(message.get("id")))
        if seq is None:
            problems.append(f"{vid} epoch {epoch} post {post}: not found on Cosmos")
        else:
            posts[f"{vid}/{epoch}/{post}"] = f"{run['url']}#msg-{seq}"
    return {"base_url": base, "runs": {name: f"{base}/runs/{slug}" for name, slug in runs.items()},
            "samples": samples, "posts": posts, "messages": messages, "problems": problems}


def _found(tools: Any, vid: str, epoch: int, post: Any) -> bool:
    try:
        find_post(tools, {"variant": vid, "epoch": epoch, "post": post})
    except Exception:  # no such post, or its log cannot be read: left unlinked
        return False
    return True


def _counted_variants(tools: Any, claim: Mapping[str, Any]) -> list[str]:
    """The variants whose counted messages a claim's links show: those of a rate or a
    comparison read from ``counts`` over the whole run (a count up to a round is not
    listed message by message)."""
    from experiments.l2.claims import basis

    if claim.get("kind") not in ("rate", "difference", "no_difference"):
        return []
    used = basis(tools, claim)
    if not used or used.get("scorer") != "counts" or used.get("rounds") is not None:
        return []
    return [claim["variant"]] if claim["kind"] == "rate" else [claim["a"], claim["b"]]


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


def _channel(get: Get, sid: int, senders: set[str]) -> tuple[str, dict[str, int]] | None:
    """A sample's channel messages on the branch Cosmos shows by default (the last
    completed one, else the last): the branch key and the ``seq`` of each message id
    sent by one of ``senders`` (the only ones the report links)."""
    branches = get(f"/api/samples/{sid}").get("branches") or []
    done = [b for b in branches if b.get("status") == "completed"]
    branch = (done or branches or [None])[-1]
    if branch is None or not SLUG.fullmatch(str(branch.get("key"))):
        return None
    key, offset, seqs = str(branch["key"]), 0, {}
    while True:
        page = get(f"/api/samples/{sid}/branches/{key}/messages?kind=channel&limit={PAGE}"
                   f"&offset={offset}")
        for m in page["items"]:
            if m.get("source_id") and str(m.get("sender")) in senders:
                seqs.setdefault(str(m["source_id"]), int(m["seq"]))
        offset += len(page["items"])
        if not page["items"] or offset >= int(page["total"]):
            return key, seqs


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
    if env := os.environ.get("COSMOS_CONFIG"):  # as the cosmos CLI's config_path()
        path = Path(env).expanduser()
    else:
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
    print(f"{len(links['samples'])} run links, {len(links['posts'])} cited messages and "
          f"{len(links['messages'])} runs' messages; report: {path}")
    for problem in links["problems"]:
        print(f"not linked: {problem}")


if __name__ == "__main__":
    main()
