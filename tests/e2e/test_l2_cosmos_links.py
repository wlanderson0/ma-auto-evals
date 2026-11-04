"""Cosmos links for an audit's claims (``experiments.l2.cosmos_links``), against a fake
Cosmos API built from the audit's own logs, as Cosmos would file them."""

from __future__ import annotations

import json

import pytest

from experiments.l2.audit_report import reload, render
from experiments.l2.cosmos_links import resolve
from experiments.l2.tools import post_ref
from tests.e2e.test_l2_audit_report import C0, V1, tools_for

BASE = "https://cosmos.example"


def fake_cosmos(tools, page=3):
    """Samples numbered from 100 in Cosmos's order; messages numbered by Cosmos from 0,
    after one message of its own (so its numbers differ from ours). Pages of ``page``."""
    results, samples, messages = tools.results(), [], {}
    for vid in sorted(results.runs):
        for s in sorted(results.of(vid), key=lambda s: s.epoch):
            sid = 100 + len(samples)
            samples.append({"id": sid, "scenario": f"{s.base}--{vid}",
                            "condition": s.condition, "epoch": s.epoch})
            posts = tools.posts(s)
            messages[sid] = [{"seq": 0, "source_id": "cosmos-own"}] + [
                {"seq": i + 1, "source_id": p["id"]} for i, p in enumerate(posts)]
    calls = []

    def get(path):
        calls.append(path)
        route, _, query = path.partition("?")
        q = dict(kv.split("=") for kv in query.split("&")) if query else {}
        lo = int(q.get("offset", 0))
        if route.startswith("/api/runs/"):
            return {"items": samples[lo:lo + page], "total": len(samples)}
        sid = int(route.split("/")[3])
        if route.endswith("/messages"):
            return {"items": messages[sid][lo:lo + page], "total": len(messages[sid])}
        return {"branches": [{"key": "branch-1", "status": "abandoned"},
                             {"key": "branch-0", "status": "completed"}]}
    return get, samples, messages, calls


@pytest.fixture
def audit(tmp_path):
    tools = tools_for(tmp_path)
    assert tools.call("run_experiment", variants=[C0, V1], epochs=2)[0]  # call 1
    assert tools.call("scores", variant_ids=["V1"], against="C0", scorer="probe")[0]  # 2
    refs = [{"variant": "V1", "epoch": e, "quote": "Status update: working",
             "post": post_ref(next(p for p in tools.posts(tools._sample("V1", e))
                                   if p["label"] == "agent:bob"))} for e in (1, 2)]
    claims = [{"text": "Bob posts status updates.", "kind": "existence", "target": "bob",
               "evidence": refs},
              {"text": "Dave: V1 and C0 are not told apart.", "kind": "no_difference",
               "a": "V1", "b": "C0", "target": "dave", "evidence": [{"call": 2}]}]
    assert tools.call("end_audit", conclusions="c", claims=claims)[0]
    return tools, refs


def test_every_sample_and_cited_post_gets_its_cosmos_link(audit):
    tools, refs = audit
    get, samples, messages, _ = fake_cosmos(tools)
    links = resolve(tools, {"exp-1": "abc123"}, get, BASE + "/")
    assert links["runs"] == {"exp-1": f"{BASE}/runs/abc123"}
    assert len(links["samples"]) == len(samples) == 4  # paged through, 3 at a time
    by_key = {(s["scenario"], s["epoch"]): s["id"] for s in samples}
    assert links["samples"]["V1/2"] == f"{BASE}/samples/{by_key[('mfa_rumor--V1', 2)]}"
    for ref in refs:
        sid = by_key[("mfa_rumor--V1", ref["epoch"])]
        post = next(p for p in tools.posts(tools._sample("V1", ref["epoch"]))
                    if post_ref(p) == ref["post"])
        seq = next(m["seq"] for m in messages[sid] if m["source_id"] == post["id"])
        assert seq != ref["post"]  # Cosmos's numbering, not ours
        assert links["posts"][f"V1/{ref['epoch']}/{ref['post']}"] == (
            f"{BASE}/samples/{sid}?branch=branch-0&kind=channel#msg-{seq}")


def test_the_report_links_each_claim_to_its_runs_and_messages(audit):
    tools, refs = audit
    get, *_ = fake_cosmos(tools)
    links = resolve(tools, {"exp-1": "abc123"}, get, BASE)
    (tools.audit.out / "cosmos.json").write_text(json.dumps(links))
    again, why = reload(tools.audit.out)
    assert why is None and again.conclusions == "c" and len(again.claims) == 2
    report = render(again)
    ref = refs[0]
    assert f"[V1 epoch {ref['epoch']} post {ref['post']}]({links['posts']['V1/1/' + str(ref['post'])]})" in report
    line = next(x for x in report.splitlines() if "dave in each run of V1" in x)
    assert f"[1 ✗]({links['samples']['V1/1']})" in line  # on the mocks Dave never adopts
    assert f"[its Cosmos run]({BASE}/runs/abc123)" in report
    assert "[open the message on Cosmos](" in report


def test_without_links_the_report_is_unchanged_and_bad_urls_are_dropped(audit):
    tools, _ = audit
    assert "On Cosmos," not in render(tools)
    (tools.audit.out / "cosmos.json").write_text(json.dumps(
        {"runs": {"exp-1": "javascript:alert(1)"}, "samples": {"V1/1": "https://x/(y)"},
         "posts": {}}))
    report = render(tools)
    assert "javascript:" not in report and "https://x/(y)" not in report


def test_a_bad_slug_is_refused(audit):
    tools, _ = audit
    get, *_ = fake_cosmos(tools)
    with pytest.raises(ValueError, match="not a Cosmos run slug"):
        resolve(tools, {"exp-1": "../../admin"}, get, BASE)
