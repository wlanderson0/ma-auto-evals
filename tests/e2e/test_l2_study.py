"""An L2 audit shared as a Cosmos study (``experiments.l2.study``, ``experiments.l2.share``):
the audit run's layout, the ``study`` every manifest names, sharing twice, and the
annotations post, against a fake Cosmos that keeps each uploaded folder as it was sent."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from inspect_ai.log import read_eval_log

from experiments.l2 import study as study_
from experiments.l2.agent import run_l2
from experiments.l2.audit import Audit
from experiments.l2.audit_report import write_report, write_writeup
from experiments.l2.brief import parse_brief
from experiments.l2.cosmos import export
from experiments.l2.cosmos_links import NotOnServer
from experiments.l2.fake import DAVE_ON_TOWN, PROMPT, fake_model
from experiments.l2.share import share
from experiments.l2.study import AUDIT_RUN, export_audit, jsonl, study_of
from experiments.l2.tools import Tools
from tests.support.l2_audit import brief

BASE = "https://cosmos.example"
TWO = [
    ("note_decision", {"field": "epochs", "value": "1", "reason": "a study test: one epoch"}),
    ("run_experiment", {"variants": json.dumps([
        {"id": "C0", "control": "M0", "rationale": "the control"},
        {"id": "V1", "control": "M0", "mutations": DAVE_ON_TOWN, "rationale": "Dave on #town"}]),
        "epochs": 1, "rationale": "a study test: the first launch"}),
    ("run_experiment", {"variants": json.dumps([
        {"id": "V2", "control": "M0", "mutations": DAVE_ON_TOWN[:1],
         "rationale": "Dave reads #town only"}]), "epochs": 1,
        "rationale": "a study test: the second launch"}),
    ("end_audit", {"conclusions": "Fake L2 (scripted): nothing is concluded.", "claims": "[]"}),
]
"""A fake L2 that runs two experiments and ends the audit."""
STUDY_FILES = ["brief.json", "claims.json", "decisions.json", "launches.json", "ledger.json",
               "proposed.json"]


@pytest.fixture(scope="module")
def ran(tmp_path_factory, offline_module):
    folder = tmp_path_factory.mktemp("study")
    # experiments run in subprocesses, as live: L2's own eval is running in this process
    audit = Audit.create(brief(folder, 1.0), folder / "board-audit", mock=True)
    run_l2(Tools(audit, on_end=write_report), fake_model(TWO), PROMPT)
    assert [e.name for e in audit.experiments()] == ["exp-1", "exp-2"]
    assert [launch["status"] for launch in jsonl(audit.out / "launches.jsonl")] == ["ran", "ran"]
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
    assert (audit / "review.json").exists()  # end_audit's reviewer writes it
    assert sorted(p.name for p in (out / "study").iterdir()) == sorted(
        STUDY_FILES + ["review.json", "writeup.json"])
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
    assert {p.suffix for p in files} == {".eval", ".json", ".html", ".md"}  # .md is skipped
    assert {p.relative_to(out).as_posix() for p in files if p.suffix == ".json"} == {
        "manifest.json", *(f"study/{p.name}" for p in (out / "study").iterdir())}
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
        assert {k: v for k, v in manifest["study"].items() if k != "probes"} == {
            **study, "experiment": exp}


def mfa_probes(*variants):
    """``study.probes`` for variants of mfa_rumor, from the scenario's own probes."""
    from experiments.scenarios import SCENARIOS

    probes = SCENARIOS["mfa_rumor"]({"target": "mockllm/model"}).probes
    return {vid: {agent: {"question": p.question, "options": list(p.options),
                          "adopted": p.adopted} for agent, p in probes.items()}
            for vid in variants}


def test_each_variants_probes_and_their_adopting_answer_are_in_the_manifest(audit, tmp_path):
    """Cosmos recounts probe claims with the answer that counts as adopting, read from
    study.probes, by variant and target agent."""
    for exp, variants in (("exp-1", ("C0", "V1")), ("exp-2", ("V2",))):
        out = export(audit / "experiments" / exp, tmp_path / exp)
        probes = json.loads((out / "manifest.json").read_text())["study"]["probes"]
        assert probes == mfa_probes(*variants)
    dave = probes["V2"]["dave"]
    assert dave["adopted"] == "NOT_REQUIRED" and dave["adopted"] in dave["options"]
    assert dave["question"].startswith("Privately, based only on what you have seen")
    assert "Answer with exactly one word" not in dave["question"]


def test_a_scenario_without_probes_leaves_them_out():
    from experiments.l2.cosmos import probes_of
    from experiments.scenarios import SCENARIOS

    models = {"target": "mockllm/model"}
    plain = SCENARIOS["kestrel_naming"](models)
    assert not plain.probes
    assert probes_of({"K1": plain}) == {}
    assert list(probes_of({"K1": plain, "M1": SCENARIOS["mfa_rumor"](models)})) == ["M1"]


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
        {**study, "experiment": "exp-1", "probes": mfa_probes("C0", "V1")},
        {**study, "experiment": "exp-2", "probes": mfa_probes("V2")},
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


def test_the_audit_run_carries_the_write_up_byte_for_byte_and_the_report(audit, tmp_path):
    """The module's audit has a write-up (the fake's last, after its review) and no
    comparison: no figures and no figures.json."""
    out = export_audit(audit, tmp_path / "out")
    assert (out / "writeup.md").read_bytes() == (audit / "writeup.md").read_bytes()
    page = (out / "report.html").read_text()
    assert page.startswith("<!doctype html>") and f"<title>{audit.name}</title>" in page
    assert "<h1>Brief</h1>" not in page and "<h1>Variants</h1>" not in page
    assert "<h2>Record</h2>" in page  # report.md's own sections
    writeup = json.loads((out / "study" / "writeup.json").read_text())
    assert writeup == {**json.loads((audit / "writeup.json").read_text()),
                       "embeds": [], "missing": []}
    lines = (audit / "writeup.md").read_text().split("\n")
    assert lines[writeup["body_line"] - 3: writeup["body_line"] - 1] == ["---", ""]
    assert lines[writeup["body_line"] - 1].startswith("Fake L2: the script is spent")
    assert writeup["final"] is True
    assert {"name": "How it ended", "value": "L2 ended the audit"} in writeup["facts"]
    assert not (out / "figures").exists() and not (out / "study" / "figures.json").exists()


def compared_and_written(audit, writeup):
    """A comparison drawn after the audit (V1 against C0, so ``figures/`` has its image
    and sidecar), and ``writeup`` as L2's final write-up; the scores call's seq."""
    tools = Tools(Audit(audit, parse_brief(audit / "brief.md")))
    tools.conclusions, tools.writeup = "concluded", writeup
    ok, _, seq = tools.numbered_call("scores", variant_ids=["V1", "C0"], against="C0",
                                     scorer="probe")
    assert ok
    write_writeup(tools)
    return tools, seq


def test_the_figures_go_with_the_numbers_they_were_drawn_from(audit, tmp_path):
    png = audit / "figures" / "V1-vs-C0-probe.png"
    (audit / "plots").mkdir()
    (audit / "outside.png").write_bytes(b"not an embed")
    tools, seq = compared_and_written(audit, "\n\n".join([
        "Results.", "![V1 vs C0](figures/V1-vs-C0-probe.png)", "![mine](plots/own.png)",
        "![gone](figures/gone.png)"]))
    shutil.copyfile(png, audit / "plots" / "own.png")  # L2's own, outside figures/
    out = export_audit(audit, tmp_path / "out")
    assert (out / "figures" / png.name).read_bytes() == png.read_bytes()
    assert (out / "plots" / "own.png").read_bytes() == png.read_bytes()
    assert not (out / "outside.png").exists()  # not embedded, not under figures/
    assert not list((out / "figures").glob("*.json"))  # sidecars are not sent
    writeup = json.loads((out / "study" / "writeup.json").read_text())
    assert writeup["embeds"] == ["figures/V1-vs-C0-probe.png", "plots/own.png",
                                 "figures/gone.png"]
    assert writeup["missing"] == ["figures/gone.png"]  # own.png is there by now
    [figure] = json.loads((out / "study" / "figures.json").read_text())
    drawn = json.loads(png.with_suffix(".json").read_text())
    compared = tools.results().compare("V1", "C0", "probe")
    assert drawn["compared"] == json.loads(json.dumps(compared))
    assert figure == {
        "path": "figures/V1-vs-C0-probe.png", "a": "V1", "b": "C0", "scorer": "probe",
        "field": None, "rounds": None, "measure": "probe", "call": seq,
        "targets": {agent: {side: {key: row[side][key] for key in ("k", "n", "incomplete")}
                            for side in ("a", "b")} for agent, row in compared.items()}}
    assert figure["targets"]


def test_the_figure_s_call_is_the_first_scores_call_that_draws_it(audit, tmp_path):
    _, first = compared_and_written(audit, "![x](figures/V1-vs-C0-probe.png)")
    compared_and_written(audit, "![x](figures/V1-vs-C0-probe.png)")  # drawn again
    out = export_audit(audit, tmp_path / "out")
    [figure] = json.loads((out / "study" / "figures.json").read_text())
    assert figure["call"] == first


def test_images_past_cosmos_limits_are_left_out_with_a_note(audit, tmp_path, monkeypatch,
                                                            capsys):
    compared_and_written(audit, "no images")
    figures = audit / "figures"
    shutil.copyfile(figures / "V1-vs-C0-probe.png", figures / "a.png")
    (figures / "B.webp").write_bytes(b"x" * (1024 * 1024 + 1))
    monkeypatch.setattr(study_, "MAX_IMAGE_BYTES", 1024 * 1024)
    monkeypatch.setattr(study_, "MAX_IMAGES", 2)  # by path: B.webp, V1-vs-C0-probe.png, a.png
    out = export_audit(audit, tmp_path / "out")
    said = capsys.readouterr().out
    assert "1 images left out of the audit run: at most 2 are sent (figures/a.png" in said
    assert "figures/B.webp left out of the audit run: an image over 1 MB" in said
    assert sorted(p.name for p in (out / "figures").iterdir()) == ["V1-vs-C0-probe.png"]
    [figure] = json.loads((out / "study" / "figures.json").read_text())
    assert figure["path"] == "figures/V1-vs-C0-probe.png"


def test_a_figure_whose_image_is_left_out_has_no_entry(audit, tmp_path, monkeypatch):
    compared_and_written(audit, "no images")
    monkeypatch.setattr(study_, "MAX_IMAGE_BYTES", 10)
    out = export_audit(audit, tmp_path / "out")
    assert not (out / "figures").exists() and not (out / "study" / "figures.json").exists()


def test_a_broken_part_is_left_out_and_never_fails_the_export(audit, tmp_path, capsys):
    compared_and_written(audit, "![x](figures/V1-vs-C0-probe.png)")
    (audit / "figures" / "V1-vs-C0-probe.json").write_text("{not json")
    (audit / "writeup.json").write_text("{}")
    out = export_audit(audit, tmp_path / "out")
    said = capsys.readouterr().out
    assert "study/figures.json left out of the audit run: JSONDecodeError" in said
    assert "study/writeup.json left out of the audit run: KeyError" in said
    assert (out / "writeup.md").exists() and (out / "figures" / "V1-vs-C0-probe.png").exists()
    assert (out / "manifest.json").exists()


def test_an_audit_written_before_writeup_json_reads_the_parts_from_writeup_md(audit,
                                                                               tmp_path):
    kept = json.loads((audit / "writeup.json").read_text())
    (audit / "writeup.json").unlink()
    out = export_audit(audit, tmp_path / "out")
    assert json.loads((out / "study" / "writeup.json").read_text()) == {
        **kept, "embeds": [], "missing": []}


def test_an_audit_without_a_write_up_or_report_exports_without_them(audit, tmp_path):
    for name in ("writeup.md", "writeup.json", "report.md"):
        (audit / name).unlink()
    out = export_audit(audit, tmp_path / "out")
    assert not (out / "writeup.md").exists() and not (out / "report.html").exists()
    assert not (out / "study" / "writeup.json").exists()
    assert (out / "manifest.json").exists()


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


@pytest.mark.parametrize("failure", ["500", "timeout"])
def test_a_failed_annotations_post_is_a_warning_after_the_links(audit, monkeypatch, failure):
    """A 5xx or a timeout on the import never stops the share: cosmos.json and the report's
    links are written first, and the warning names the run and the status only."""
    import httpx

    from experiments.l2 import cosmos_links

    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(500, text="secret-looking body not-a-real-token")
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(handler), **kw))
    annotations(audit, RECORDS[:1])
    annotations(audit / "experiments" / "exp-2", RECORDS[1:2])
    cosmos = FakeCosmos()
    post = cosmos_links.http_post(BASE, "not-a-real-token")
    said = []
    links = share(audit, upload=cosmos.upload, get=cosmos.get, post=post, base_url=BASE,
                  sleep=lambda s: None, say=said.append)
    assert json.loads((audit / "cosmos.json").read_text()) == links  # the links are written
    assert links["runs"] == {"exp-1": f"{BASE}/runs/run0", "exp-2": f"{BASE}/runs/run1"}
    assert any(s.startswith("report with Cosmos links:") for s in said)  # and relinked
    why = "HTTP 500" if failure == "500" else "ReadTimeout"
    assert f"annotations for exp-1 (run0) not posted: {why}; share again to post them" in said
    assert f"annotations for exp-2 (run1) not posted: {why}; share again to post them" in said
    assert said[-1] == "annotations: 0 created, 0 unchanged, 0 not imported, 2 batches not posted"
    assert not any("not-a-real-token" in s or "body" in s for s in said)
    assert said.index("annotations for exp-1 (run0) not posted: "
                      f"{why}; share again to post them") > next(
        i for i, s in enumerate(said) if s.startswith("report with Cosmos links"))
