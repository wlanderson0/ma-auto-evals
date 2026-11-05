"""Cosmos links for an audit's claims (``experiments.l2.cosmos_links``), against a fake
Cosmos API built from the audit's own logs, as Cosmos would file them."""

from __future__ import annotations

import json

import pytest

from experiments.l2.audit_report import relink, render
from experiments.l2.cosmos_links import resolve
from experiments.l2.tools import post_ref
from tests.e2e.test_l2_audit_report import C0, V1, tools_for

BASE = "https://cosmos.example"


def fake_cosmos(tools, page=3, fail=None):
    """One run, the audit's exp-1, with its manifest; samples numbered from 100; messages
    numbered by Cosmos from 0, after one message of its own (so its numbers differ from
    ours), in pages of ``page``. ``fail``: a path prefix that answers with an error."""
    results, samples, messages = tools.results(), [], {}
    for vid in sorted(results.runs):
        for s in sorted(results.of(vid), key=lambda s: s.epoch):
            sid = 100 + len(samples)
            samples.append({"id": sid, "scenario": f"{s.base}--{vid}",
                            "condition": s.condition, "epoch": s.epoch})
            posts = tools.posts(s)
            messages[sid] = [{"seq": 0, "source_id": "cosmos-own", "sender": "system"}] + [
                {"seq": i + 1, "source_id": p["id"], "sender": p["sender"]}
                for i, p in enumerate(posts)]
    calls = []
    decisions = json.loads(next(tools.audit.out.glob("experiments/exp-1/decisions.json"))
                           .read_text())
    manifest = {"sha": (decisions.get("provenance") or {}).get("commit"),
                "started_at": decisions.get("started")}

    def get(path):
        calls.append(path)
        if fail and path.startswith(fail):
            raise RuntimeError("HTTP 503")
        route, _, query = path.partition("?")
        q = dict(kv.split("=") for kv in query.split("&")) if query else {}
        lo = int(q.get("offset", 0))
        if route.startswith("/api/runs/"):
            return {"manifest": manifest, "samples": samples}
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
    assert len(links["samples"]) == len(samples) == 4 and links["problems"] == []
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
    before = (tools.audit.out / "report.md").read_text()
    report = relink(tools.audit.out).read_text()
    # only the claims section changes: everything before and after it is kept
    head, tail = before.split("## Claims, checked")[0], before.split("\n## Conclusions")[1]
    assert report.startswith(head) and report.endswith(tail)
    ref = refs[0]
    assert f"[V1 epoch {ref['epoch']} post {ref['post']}]({links['posts']['V1/1/' + str(ref['post'])]})" in report
    line = next(x for x in report.splitlines() if "dave in each run of V1, as call 2" in x)
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


def test_a_slug_that_is_not_the_experiments_run_is_refused(audit, monkeypatch):
    tools, _ = audit
    get, samples, *_ = fake_cosmos(tools)

    def other_commit(path):
        out = get(path)
        return {**out, "manifest": {**out["manifest"], "sha": "0" * 40}} if (
            "manifest" in out) else out
    with pytest.raises(ValueError, match="its commit is"):
        resolve(tools, {"exp-1": "abc123"}, other_commit, BASE)

    def fewer_samples(path):
        out = get(path)
        return {**out, "samples": out["samples"][1:]} if "samples" in out else out
    with pytest.raises(ValueError, match="its samples differ"):
        resolve(tools, {"exp-1": "abc123"}, fewer_samples, BASE)
    with pytest.raises(ValueError, match="not an experiment"):
        resolve(tools, {"exp-9": "abc123"}, get, BASE)


def test_marks_come_from_the_call_the_verdict_read(tmp_path):
    """A claim citing a post_judge table first and the probe comparison second is checked
    on the probe; its marks are the probe's too."""
    from experiments.l2.claims import check_claims, parse_claims

    tools = tools_for(tmp_path)
    assert tools.call("run_experiment", variants=[C0, V1], epochs=2)[0]  # call 1
    assert tools.call("scores", variant_ids=["V1"], scorer="post_judge")[0]  # 2
    assert tools.call("scores", variant_ids=["V1"], against="C0", scorer="probe")[0]  # 3
    claim = {"text": "Dave: 0/2 in V1.", "kind": "rate", "variant": "V1", "target": "dave",
             "k": 0, "n": 2, "evidence": [{"call": 2}, {"call": 3}]}
    [checked] = check_claims(tools, parse_claims([claim], tools))
    assert checked["verdict"] == "checked" and checked["basis"]["call"] == 3
    tools.claims = [checked]
    get, *_ = fake_cosmos(tools)
    links = resolve(tools, {"exp-1": "abc123"}, get, BASE)
    (tools.audit.out / "cosmos.json").write_text(json.dumps(
        {**links, "samples": {"V1/1": links["samples"]["V1/1"]}}))  # epoch 2 not linked
    line = next(x for x in render(tools).splitlines() if "dave in each run of V1" in x)
    assert "as call 3 read it" in line and "?" not in line
    assert f"[1 ✗]({links['samples']['V1/1']})" in line and "2 ✗ (not on Cosmos)" in line


def test_an_error_on_one_message_is_recorded_not_fatal(audit):
    tools, _ = audit
    get, *_ = fake_cosmos(tools, fail="/api/samples/")
    links = resolve(tools, {"exp-1": "abc123"}, get, BASE)
    assert len(links["samples"]) == 4 and links["posts"] == {}
    assert len(links["problems"]) == 2 and "HTTP 503" in links["problems"][0]


def test_a_malformed_cosmos_json_is_ignored(audit):
    tools, _ = audit
    (tools.audit.out / "cosmos.json").write_text("[1, 2]")
    assert "## Claims, checked against the logs" in render(tools)
    (tools.audit.out / "cosmos.json").write_text('{"samples": [1], "runs": "x"}')
    assert "## Ledger" in render(tools)


def test_a_count_links_every_message_it_counted(audit, monkeypatch):
    """A claim read from counts lists, per run, each post that counted (the target's own
    uses of the name; a post copying another agent's counts 0), each linked to its
    message on Cosmos, when they add up to the recorded count. The mock posts' "Status"
    stands in for the scenario's name, and the recorded counts are set to match it."""
    import dataclasses

    from experiments.l2.results import Results
    from experiments.measures import own_mentions_by_post

    tools, _ = audit
    real = Results.scenario
    monkeypatch.setattr(Results, "scenario", lambda self, base: dataclasses.replace(
        real(self, base), mention=r"Status"))
    samples_v1 = sorted(tools.results().of("V1"), key=lambda s: s.epoch)
    own = {s.epoch: sum(h for _, h in own_mentions_by_post(r"Status", "bob", tools.posts(s)))
           for s in samples_v1}
    assert all(own.values())  # Bob's first status line is his own in each run
    recorded = {1: own[1], 2: own[2] + 1}  # epoch 2's record disagrees with its posts
    for s in samples_v1:
        s.scores["counts"] = {"final": "b", "branches": {"b": {"agents": {
            "bob": {"own_mentions": recorded[s.epoch]}}}}}
    tools.claims = [{"text": "Bob says it.", "kind": "rate", "variant": "V1", "target": "bob",
                     "k": 2, "n": 2, "evidence": [{"call": 2}], "verdict": "checked",
                     "reasons": [], "basis": {"call": 2, "scorer": "counts", "rounds": None,
                                              "field": None}}]
    get, samples, messages, _ = fake_cosmos(tools)
    links = resolve(tools, {"exp-1": "abc123"}, get, BASE)
    assert set(links["messages"]) == {"V1/1", "V1/2"} and links["problems"] == []
    assert all(len(r["seq"]) < len(messages[100]) for r in links["messages"].values())
    (tools.audit.out / "cosmos.json").write_text(json.dumps(links))
    lines = render(tools).splitlines()
    marks = next(x for x in lines if "bob in each run of V1" in x)
    listed = next(x for x in lines if "The messages counted in V1" in x)
    off = next(x for x in lines if "Messages not listed in V1" in x)
    assert listed.startswith("   - The messages") and off.startswith("   - Messages")
    assert "[1 ✓]" in marks and "[2 ✓]" in marks
    sid = next(s["id"] for s in samples if (s["scenario"], s["epoch"]) == ("mfa_rumor--V1", 1))
    posts = tools.posts(samples_v1[0])
    for post, hits in own_mentions_by_post(r"Status", "bob", posts):
        seq = next(m["seq"] for m in messages[sid] if m["source_id"] == post["id"])
        link = f"[post {post_ref(post)}]({BASE}/samples/{sid}?branch=branch-0&kind=channel#msg-{seq})"
        assert (link in listed) == bool(hits)  # counted posts only, copies left out
    assert "epoch 2:" not in listed and f"epoch 2 (recorded {own[2] + 1} uses" in off


def test_a_run_with_no_recorded_count_lists_no_messages(audit, monkeypatch):
    """The commit's first version listed messages for runs whose count is unknown."""
    import dataclasses

    from experiments.l2.results import Results

    tools, _ = audit
    real = Results.scenario
    monkeypatch.setattr(Results, "scenario", lambda self, base: dataclasses.replace(
        real(self, base), mention=r"Status"))
    tools.claims = [{"text": "Bob.", "kind": "rate", "variant": "V1", "target": "bob", "k": 0,
                     "n": 0, "evidence": [{"call": 2}], "verdict": "checked", "reasons": [],
                     "basis": {"call": 2, "scorer": "counts", "rounds": None, "field": None}}]
    get, *_ = fake_cosmos(tools)
    (tools.audit.out / "cosmos.json").write_text(json.dumps(
        resolve(tools, {"exp-1": "abc123"}, get, BASE)))
    text = render(tools)
    assert "[1 ?]" in text and "The messages counted" not in text  # mfa has no recorded count


def test_share_exports_uploads_waits_and_links_in_one_step(audit, tmp_path):
    """Each experiment is exported and uploaded once (its slug kept in cosmos-runs.json),
    the runs are waited on until ready, and the report gets its links. A second call
    uploads nothing."""
    from experiments.l2.share import share

    tools, _ = audit
    get, *_ = fake_cosmos(tools)
    uploads, polls = [], {}

    def upload(folder, name, pr):
        assert (folder / "manifest.json").exists() and name.endswith(" exp-1")
        uploads.append(folder)
        return "abc123"

    def cosmos(path):
        if path == "/api/runs/abc123":  # parsing until polled a few times, then ready
            polls[path] = polls.get(path, 0) + 1
            return {**get(path), "status": "ready" if polls[path] > 3 else "parsing"}
        return get(path)
    said = []
    links = share(tools.audit.out, upload=upload, get=cosmos, base_url=BASE,
                  sleep=lambda s: None, say=said.append)
    assert len(uploads) == 1 and links["runs"] == {"exp-1": f"{BASE}/runs/abc123"}
    assert uploads[0] == tools.audit.out / "cosmos" / "exp-1"  # inside the audit
    assert not [p for p in uploads[0].parent.iterdir() if p.name.startswith(".")]
    assert json.loads((tools.audit.out / "cosmos-runs.json").read_text()) == {"exp-1": "abc123"}
    assert "dave in each run of V1" in (tools.audit.out / "report.md").read_text()
    assert said[0] == f"exp-1: {BASE}/runs/abc123" and "report with Cosmos links" in said[1]
    share(tools.audit.out, upload=upload, get=cosmos, base_url=BASE, sleep=lambda s: None,
          say=said.append)
    assert len(uploads) == 1  # nothing uploaded twice


def test_share_stops_on_a_run_cosmos_could_not_parse(audit):
    from experiments.l2.share import share

    tools, _ = audit
    get, *_ = fake_cosmos(tools)
    with pytest.raises(SystemExit, match="could not parse"):
        share(tools.audit.out, upload=lambda f, n, p: "abc123",
              get=lambda path: {**get(path), "status": "failed"}, base_url=BASE,
              sleep=lambda s: None, say=lambda s: None)


def test_a_run_that_is_not_the_experiments_is_never_recorded(audit):
    """E.g. another audit's export uploaded by mistake: refused before it is kept, so a
    rerun uploads afresh."""
    from experiments.l2.share import share

    tools, _ = audit
    get, *_ = fake_cosmos(tools)

    def other(path):
        out = get(path)
        return {**out, "manifest": {**out["manifest"], "started_at": "2020-01-01T00:00:00+00:00"},
                "status": "ready"}
    with pytest.raises(SystemExit, match="was not recorded"):
        share(tools.audit.out, upload=lambda f, n, p: "abc123", get=other, base_url=BASE,
              sleep=lambda s: None, say=lambda s: None)
    assert not (tools.audit.out / "cosmos-runs.json").exists()


def test_any_failure_while_sharing_is_a_message_not_a_traceback(audit, capsys):
    from experiments.l2.share import share_or_explain

    tools, _ = audit

    def unreachable(path):
        raise ConnectionError("cosmos.example unreachable")
    assert not share_or_explain(tools.audit.out, upload=lambda f, n, p: "abc123",
                                get=unreachable, base_url=BASE, sleep=lambda s: None,
                                say=lambda s: None)
    out = capsys.readouterr().out
    assert "not shared on Cosmos: ConnectionError" in out and "experiments.l2.share" in out


def test_a_failed_export_leaves_nothing_to_reuse(audit, monkeypatch):
    import experiments.l2.cosmos as cosmos
    from experiments.l2.share import share_or_explain

    tools, _ = audit

    def broken(exp, out, pr=None):
        out.mkdir(parents=True)
        (out / "manifest.json").write_text("{}")
        raise OSError("disk full")
    monkeypatch.setattr(cosmos, "export", broken)
    assert not share_or_explain(tools.audit.out, upload=lambda f, n, p: "abc123",
                                get=lambda p: {}, base_url=BASE, say=lambda s: None)
    assert not (tools.audit.out / "cosmos" / "exp-1").exists()


def test_sign_in_reads_the_config_the_cosmos_cli_reads(tmp_path, monkeypatch):
    from experiments.l2.cosmos_links import sign_in

    config = tmp_path / "elsewhere.json"
    config.write_text(json.dumps({"url": "https://cosmos.example", "token": "fake-token"}))
    monkeypatch.delenv("COSMOS_URL", raising=False)
    monkeypatch.delenv("COSMOS_TOKEN", raising=False)
    monkeypatch.setenv("COSMOS_CONFIG", str(config))
    assert sign_in() == ("https://cosmos.example", "fake-token")
