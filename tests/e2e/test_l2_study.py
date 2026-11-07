"""An L2 audit shared as a Cosmos study (``experiments.l2.study``, ``experiments.l2.share``):
the audit run's layout, the ``study`` every manifest names, sharing twice, and the
annotations post, against a fake Cosmos that keeps each uploaded folder as it was sent."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from inspect_ai.log import read_eval_log

from experiments.l2.agent import run_l2
from experiments.l2.audit import Audit
from experiments.l2.audit_report import write_report
from experiments.l2.cosmos import export
from experiments.l2.cosmos_links import NotOnServer
from experiments.l2.fake import DAVE_ON_TOWN, PROMPT, fake_model
from experiments.l2.share import share
from experiments.l2.study import AUDIT_RUN, export_audit, jsonl, study_of
from experiments.l2.tools import Tools
from tests.support.l2_audit import InProcess, brief

BASE = "https://cosmos.example"
TWO = [
    ("note_decision", {"field": "epochs", "value": "1", "reason": "a study test: one epoch"}),
    ("run_experiment", {"variants": json.dumps([
        {"id": "C0", "control": "M0", "rationale": "the control"},
        {"id": "V1", "control": "M0", "mutations": DAVE_ON_TOWN, "rationale": "Dave on #town"}]),
        "epochs": 1}),
    ("run_experiment", {"variants": json.dumps([
        {"id": "V2", "control": "M0", "mutations": DAVE_ON_TOWN[:1],
         "rationale": "Dave reads #town only"}]), "epochs": 1}),
    ("end_audit", {"conclusions": "Fake L2 (scripted): nothing is concluded.", "claims": "[]"}),
]
"""A fake L2 that runs two experiments and ends the audit."""
STUDY_FILES = ["brief.json", "claims.json", "decisions.json", "launches.json", "ledger.json",
               "proposed.json"]


@pytest.fixture(scope="module")
def ran(tmp_path_factory, offline_module):
    folder = tmp_path_factory.mktemp("study")
    audit = Audit.create(brief(folder, 1.0), folder / "board-audit", mock=True,
                         launch=InProcess())
    run_l2(Tools(audit, on_end=write_report), fake_model(TWO), PROMPT)
    assert [e.name for e in audit.experiments()] == ["exp-1", "exp-2"]
    return audit.out


@pytest.fixture
def audit(ran, tmp_path):
    """A copy of the module's audit, for a test to share and change."""
    return Path(shutil.copytree(ran, tmp_path / ran.name))


class FakeCosmos:
    """Keeps each uploaded folder's manifest and samples (from its results rows), as Cosmos
    would file them; every run is ready at once. ``annotations``: the import's answer (or
    a function of the request's body giving it), or an exception to raise (an older
    server)."""

    def __init__(self, annotations=None):
        self.runs: dict[str, dict] = {}
        self.uploads: list[tuple[Path, str]] = []
        self.posts: list[tuple[str, dict]] = []
        self.annotations = annotations

    def upload(self, folder, name, pr):
        slug = f"run{len(self.uploads)}"
        self.uploads.append((Path(folder), name))
        manifest = json.loads((Path(folder) / "manifest.json").read_text())
        rows = []
        if (Path(folder) / "results.json").exists():
            rows = json.loads((Path(folder) / "results.json").read_text())["results"]
        self.runs[slug] = {"manifest": manifest, "status": "ready", "samples": [
            {"id": 100 * len(self.uploads) + i, "scenario": r["scenario"],
             "condition": r["condition"], "epoch": r["epoch"]} for i, r in enumerate(rows)]}
        return slug

    def get(self, path):
        return self.runs[path.removeprefix("/api/runs/")]

    def post(self, path, body):
        self.posts.append((path, body))
        if isinstance(self.annotations, Exception):
            raise self.annotations
        if callable(self.annotations):
            return self.annotations(body)
        return self.annotations or {"created": len(body["records"]), "unchanged": 0,
                                    "problems": []}

    def share(self, audit, say=None):
        said = [] if say is None else say
        links = share(audit, upload=self.upload, get=self.get, post=self.post, base_url=BASE,
                      sleep=lambda s: None, say=said.append)
        return links, said


def test_the_audit_run_holds_the_manifest_l2s_log_and_the_study_files(audit, tmp_path):
    out = export_audit(audit, tmp_path / "out", pr=7)
    manifest = json.loads((out / "manifest.json").read_text())
    study = study_of(audit)
    assert manifest["study"] == {**study, "role": "audit"}
    assert manifest["schema"] == "cosmos.manifest/1" and manifest["pr"] == 7
    decisions = json.loads((audit / "experiments" / "exp-1" / "decisions.json").read_text())
    assert manifest["sha"] == decisions["provenance"]["commit"]
    [log] = (audit / "l2").glob("*.eval")
    assert (out / "l2" / log.name).read_bytes() == log.read_bytes()  # unchanged
    header = read_eval_log(str(log), header_only=True)
    assert manifest["models"] == {"l2": "mockllm/l2"}
    assert (manifest["started_at"], manifest["finished_at"]) == (
        header.stats.started_at, header.stats.completed_at)
    assert sorted(p.name for p in (out / "study").iterdir()) == STUDY_FILES
    read = {p.stem: json.loads(p.read_text()) for p in (out / "study").iterdir()}
    assert read["brief"] == {"text": (audit / "brief.md").read_text()}
    assert read["claims"] == json.loads((audit / "claims.json").read_text())
    assert read["ledger"] == json.loads((audit / "ledger.json").read_text())
    for name in ("decisions", "proposed", "launches"):  # the JSONL files as JSON arrays
        assert read[name] == jsonl(audit / f"{name}.jsonl") and read[name], name
    assert [launch.get("experiment") for launch in read["launches"]
            if launch.get("experiment")] == ["exp-1", "exp-2"]


def test_older_cosmos_servers_accept_the_audit_run(audit, tmp_path):
    """Cosmos before studies takes ``.eval``, ``.json`` and ``.html`` files, keeps the
    manifest as given, and reads a ``.json`` as results only with a ``results`` list, or
    as findings when it is a list of ``title``/``body`` items: none of study/ is either."""
    out = export_audit(audit, tmp_path / "out")
    files = [p for p in out.rglob("*") if p.is_file()]
    assert {p.suffix for p in files} == {".eval", ".json"}
    for path in (out / "study").iterdir():
        data = json.loads(path.read_text())
        assert not path.name.startswith("results") and "findings" not in path.name
        assert not (isinstance(data, dict) and isinstance(data.get("results"), list))
        assert not (isinstance(data, list) and data
                    and all(isinstance(i, dict) and {"title", "body"} <= set(i) for i in data))


def test_every_experiments_manifest_names_the_study(audit, tmp_path):
    study = study_of(audit)
    header = read_eval_log(str(next((audit / "l2").glob("*.eval"))), header_only=True)
    assert study == {"id": f"board-audit@{header.stats.started_at}", "name": "board-audit"}
    assert study_of(audit) == study  # stable for the audit
    for exp in ("exp-1", "exp-2"):
        out = export(audit / "experiments" / exp, tmp_path / exp)
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["study"] == {**study, "experiment": exp}


def test_an_audit_without_l2s_log_is_dated_by_its_first_experiment(audit, tmp_path):
    shutil.rmtree(audit / "l2")
    decisions = json.loads((audit / "experiments" / "exp-1" / "decisions.json").read_text())
    study = study_of(audit)
    assert study["id"].startswith("board-audit@") and study["id"] != "board-audit@None"
    assert study["id"][len("board-audit@"):][:10] == decisions["started"][:10]
    out = export_audit(audit, tmp_path / "out")
    assert not list((out / "l2").iterdir())
    assert json.loads((out / "manifest.json").read_text())["models"] == {}


def test_share_uploads_the_audit_run_beside_the_experiments_once(audit):
    cosmos = FakeCosmos()
    links, said = cosmos.share(audit)
    assert [name for _, name in cosmos.uploads] == [
        "board-audit exp-1", "board-audit exp-2", "board-audit audit"]
    assert [folder for folder, _ in cosmos.uploads] == [
        audit / "cosmos" / "exp-1", audit / "cosmos" / "exp-2", audit / "cosmos" / AUDIT_RUN]
    record = json.loads((audit / "cosmos-runs.json").read_text())
    assert record == {"exp-1": "run0", "exp-2": "run1", "audit": "run2"}
    study = study_of(audit)
    assert [cosmos.runs[s]["manifest"]["study"] for s in ("run0", "run1", "run2")] == [
        {**study, "experiment": "exp-1"}, {**study, "experiment": "exp-2"},
        {**study, "role": "audit", "runs": {"exp-1": "run0", "exp-2": "run1"}}]
    assert links["runs"] == {"exp-1": f"{BASE}/runs/run0", "exp-2": f"{BASE}/runs/run1"}
    assert f"audit: {BASE}/runs/run2" in said
    assert cosmos.posts == []  # no annotations.jsonl: nothing posted
    assert not [p for p in (audit / "cosmos").iterdir() if p.name.startswith(".")]
    cosmos.share(audit)  # again: nothing is uploaded twice
    assert len(cosmos.uploads) == 3


def test_an_export_made_before_studies_gains_the_study_before_upload(audit):
    folder = audit / "cosmos" / "exp-1"
    export(audit / "experiments" / "exp-1", folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    del manifest["study"]
    (folder / "manifest.json").write_text(json.dumps(manifest))
    cosmos = FakeCosmos()
    cosmos.share(audit)
    assert cosmos.runs["run0"]["manifest"]["study"]["experiment"] == "exp-1"


def test_a_run_that_is_not_this_audits_study_is_never_recorded(audit):
    cosmos = FakeCosmos()

    def upload(folder, name, pr):
        slug = cosmos.upload(folder, name, pr)
        if name.endswith(" audit"):
            cosmos.runs[slug]["manifest"]["study"]["id"] = "another@2020-01-01"
        return slug
    with pytest.raises(SystemExit, match="not this audit's run"):
        share(audit, upload=upload, get=cosmos.get, post=cosmos.post, base_url=BASE,
              sleep=lambda s: None, say=lambda s: None)
    assert AUDIT_RUN not in json.loads((audit / "cosmos-runs.json").read_text())


def test_review_json_is_copied_when_the_audit_has_one(audit, tmp_path):
    review = {"reviewer": "someone", "notes": ["read the V1 runs"]}
    (audit / "review.json").write_text(json.dumps(review))
    out = export_audit(audit, tmp_path / "out")
    assert json.loads((out / "study" / "review.json").read_text()) == review


def annotations(audit, records):
    (audit / "annotations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


RECORDS = [
    {"id": "a-1", "kind": "comment", "target": {"type": "sample", "variant": "V1", "epoch": 1},
     "body": "Dave reads #town here.", "author": "l2", "at": "2026-11-05T12:00:00Z"},
    {"id": "a-2", "kind": "tag", "target": {"type": "message", "variant": "V2", "epoch": 1,
                                            "branch": "branch-0", "seq": 3},
     "tags": ["off-dose"], "author": "l2"},
    {"id": "a-3", "kind": "tag", "target": {"type": "run", "scenario": "mfa_rumor--C0"},
     "tags": ["control"], "author": "sarah"},
    {"id": "a-4", "kind": "comment", "target": {"type": "run"}, "body": "The whole audit."},
    {"id": "a-5", "kind": "comment", "target": {"type": "sample", "variant": "V9", "epoch": 1},
     "body": "No such variant."},
]


def test_annotations_are_posted_to_the_runs_that_hold_their_targets(audit):
    annotations(audit, RECORDS)
    def answer(body):
        ids = [r["id"] for r in body["records"]]
        problems = [{"id": "a-2", "reason": "no message #3"}] if "a-2" in ids else []
        return {"created": len(ids) - len(problems), "unchanged": 0, "problems": problems}
    cosmos = FakeCosmos(answer)
    _, said = cosmos.share(audit)
    posted = {path: [r["id"] for r in body["records"]] for path, body in cosmos.posts}
    assert posted == {"/api/runs/run0/annotations": ["a-1", "a-3"],  # exp-1 ran C0 and V1
                      "/api/runs/run1/annotations": ["a-2"],  # exp-2 ran V2
                      "/api/runs/run2/annotations": ["a-4"]}  # no variant: the audit run
    body = cosmos.posts[0][1]["records"][0]
    assert body == RECORDS[0]  # sent in spec 13's shape, as written
    assert any("a-5" in s and "'V9'" in s for s in said)
    assert any("a-2 not imported on exp-2: no message #3" in s for s in said)
    assert "annotations: 3 created, 0 unchanged, 2 not imported" in said


def test_annotations_are_posted_again_on_a_second_share_and_cosmos_keeps_them_once(audit):
    annotations(audit, RECORDS[:1])
    cosmos = FakeCosmos()
    cosmos.share(audit)
    cosmos.annotations = {"created": 0, "unchanged": 1, "problems": []}
    _, said = cosmos.share(audit)
    assert len(cosmos.uploads) == 3 and len(cosmos.posts) == 2
    assert "annotations: 0 created, 1 unchanged, 0 not imported" in said


def test_an_older_cosmos_without_the_import_is_a_warning(audit):
    annotations(audit, RECORDS[:2])
    cosmos = FakeCosmos(NotOnServer("HTTP 404 on POST /api/runs/run0/annotations"))
    links, said = cosmos.share(audit)
    assert len(cosmos.posts) == 1  # it stops asking after the first 404
    assert any(s.startswith("annotations not posted: this Cosmos cannot import them (HTTP 404")
               for s in said)
    assert links["runs"]["exp-1"] == f"{BASE}/runs/run0"  # shared all the same


def test_an_experiments_own_annotations_go_to_its_run(audit):
    """L2 spec §6.2: annotations.jsonl next to an experiment's logs is that experiment's,
    whatever its records name; the audit's own file is routed by variant beside it."""
    mine = [{**RECORDS[3], "id": "e-1"}, {**RECORDS[0], "id": "e-2"}]  # no variant; V1
    annotations(audit / "experiments" / "exp-2", mine)
    annotations(audit, RECORDS[:1])
    cosmos = FakeCosmos()
    cosmos.share(audit)
    posted = {path: [r["id"] for r in body["records"]] for path, body in cosmos.posts}
    assert posted == {"/api/runs/run0/annotations": ["a-1"],
                      "/api/runs/run1/annotations": ["e-1", "e-2"]}


def test_resharing_an_audit_shared_before_studies_makes_it_one(audit, tmp_path):
    """Its experiments are on Cosmos already, with manifests that name no study: only the
    audit run is uploaded, and its manifest lists their slugs (study.runs), so Cosmos can
    join them to the study."""
    cosmos = FakeCosmos()
    old = {}
    for exp in ("exp-1", "exp-2"):
        folder = export(audit / "experiments" / exp, tmp_path / exp)
        manifest = json.loads((folder / "manifest.json").read_text())
        del manifest["study"]
        (folder / "manifest.json").write_text(json.dumps(manifest))
        old[exp] = f"old{exp[-1]}"
        cosmos.runs[old[exp]] = cosmos_run(folder)
    (audit / "cosmos-runs.json").write_text(json.dumps(old))
    links, _ = cosmos.share(audit)
    assert [name for _, name in cosmos.uploads] == ["board-audit audit"]
    [(folder, _)] = cosmos.uploads
    study = json.loads((folder / "manifest.json").read_text())["study"]
    assert study == {**study_of(audit), "role": "audit", "runs": old}
    assert json.loads((audit / "cosmos-runs.json").read_text()) == {**old, "audit": "run0"}
    assert links["runs"] == {exp: f"{BASE}/runs/{slug}" for exp, slug in old.items()}


def cosmos_run(folder):
    """A run as Cosmos keeps an uploaded experiment folder."""
    rows = json.loads((folder / "results.json").read_text())["results"]
    return {"manifest": json.loads((folder / "manifest.json").read_text()), "status": "ready",
            "samples": [{"id": 900 + i, "scenario": r["scenario"], "condition": r["condition"],
                         "epoch": r["epoch"]} for i, r in enumerate(rows)]}


def test_the_http_layer_turns_404_and_405_into_not_on_server(monkeypatch):
    import httpx

    from experiments.l2 import cosmos_links

    statuses = iter([404, 405, 200])

    def handler(request):
        assert request.url.path == "/api/runs/abc/annotations"
        assert json.loads(request.content) == {"records": []}
        status = next(statuses)
        return httpx.Response(status, json={"created": 0, "unchanged": 0, "problems": []})
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(handler), **kw))
    post = cosmos_links.http_post(BASE, "not-a-real-token")
    for status in (404, 405):
        with pytest.raises(NotOnServer, match=f"HTTP {status}"):
            post("/api/runs/abc/annotations", {"records": []})
    assert post("/api/runs/abc/annotations", {"records": []})["created"] == 0
