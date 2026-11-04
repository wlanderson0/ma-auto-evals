"""Cosmos links for an L2 audit's claims, resolved after its experiments are uploaded.

Usage::

    uv run python -m experiments.l2.cosmos_links <audit> --run exp-1=<slug> [--run ...]

Each ``--run`` names one of the audit's experiments and the slug of its Cosmos run (the
end of the link ``cosmos upload`` printed). Cosmos gives a sample its page, and a message
its place in the transcript, only when the run is parsed, so the links cannot be written
before the upload. This command asks Cosmos for them, with the same sign-in as the
``cosmos`` CLI (``COSMOS_URL`` and ``COSMOS_TOKEN``, else ``cosmos login``'s config),
saves them in the audit as ``cosmos.json``, and writes the report again with them:

- every sample of a variant a claim rests on: ``<server>/samples/<id>``, so a rate or a
  comparison links to each run behind its count, marked with what that run showed;
- every post a claim cites: ``<server>/samples/<id>?branch=<b>&kind=channel#msg-<seq>``,
  the message itself, matched by its message id (Cosmos's ``source_id``) on the sample's
  adopted branch.

Only the report and ``cosmos.json`` are written; the record of the audit is not changed.
The report on Cosmos (its "Open report") is the one uploaded and has no links.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Mapping
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
    experiment's name (``exp-1``) to its Cosmos run's slug."""
    base = base_url.rstrip("/")
    for name, slug in runs.items():
        if not SLUG.fullmatch(slug):
            raise ValueError(f"{name}: {slug!r} is not a Cosmos run slug")
    results = tools.results()
    found: dict[tuple[str, str, int], int] = {}  # (scenario, condition, epoch) -> sample id
    for slug in runs.values():
        offset = 0
        while True:
            page = get(f"/api/runs/{slug}/samples?limit={PAGE}&offset={offset}")
            for item in page["items"]:
                found[(str(item["scenario"]), str(item["condition"]), int(item["epoch"]))] = int(
                    item["id"])
            offset += len(page["items"])
            if not page["items"] or offset >= int(page["total"]):
                break
    samples: dict[str, str] = {}
    for vid in sorted(results.runs):
        if tools._where.get(vid) not in runs:
            continue
        for sample in results.of(vid):
            sid = found.get((f"{sample.base}--{vid}", sample.condition, sample.epoch))
            if sid is not None:
                samples[f"{vid}/{sample.epoch}"] = f"{base}/samples/{sid}"
    posts: dict[str, str] = {}
    refs = {(r["variant"], r["epoch"], r["post"]) for claim in tools.claims or []
            for r in claim["evidence"] if "call" not in r}
    for vid, epoch, post in sorted(refs, key=str):
        url = samples.get(f"{vid}/{epoch}")
        if url is None:
            continue
        sid = int(url.rsplit("/", 1)[1])
        message = find_post(tools, {"variant": vid, "epoch": epoch, "post": post})
        detail = get(f"/api/samples/{sid}")
        branches = detail.get("branches") or []
        branch = next((b for b in branches if b.get("status") == "completed"),
                      branches[0] if branches else None)
        if branch is None or not SLUG.fullmatch(str(branch.get("key"))):
            continue
        key, offset, seq = str(branch["key"]), 0, None
        while seq is None:
            page = get(f"/api/samples/{sid}/branches/{key}/messages?kind=channel&limit={PAGE}"
                       f"&offset={offset}")
            seq = next((int(m["seq"]) for m in page["items"]
                        if m.get("source_id") == message.get("id")), None)
            offset += len(page["items"])
            if not page["items"] or offset >= int(page["total"]):
                break
        if seq is not None:
            query = urlencode({"branch": key, "kind": "channel"})
            posts[f"{vid}/{epoch}/{post}"] = f"{base}/samples/{sid}?{query}#msg-{seq}"
    return {"base_url": base, "runs": {name: f"{base}/runs/{slug}" for name, slug in runs.items()},
            "samples": samples, "posts": posts}


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
    from experiments.l2.audit_report import reload, write_report

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("audit", type=Path)
    parser.add_argument("--run", action="append", default=[], metavar="EXPERIMENT=SLUG",
                        help="an experiment and its Cosmos run's slug, e.g. exp-2=pd23mtf5")
    args = parser.parse_args(argv)
    runs = dict(item.split("=", 1) for item in args.run)
    if not runs:
        parser.error("give at least one --run exp-<n>=<slug>")
    tools, why = reload(args.audit)
    unknown = set(runs) - {p.name for p in tools.audit.experiments()}
    if unknown:
        parser.error(f"{sorted(unknown)} are not experiments of {args.audit}")
    url, token = sign_in()
    links = resolve(tools, runs, http_get(url, token), url)
    (tools.audit.out / "cosmos.json").write_text(json.dumps(links, indent=2) + "\n")
    path = write_report(tools, why)
    print(f"{len(links['samples'])} sample links and {len(links['posts'])} message links; "
          f"report: {path}")


if __name__ == "__main__":
    main()
