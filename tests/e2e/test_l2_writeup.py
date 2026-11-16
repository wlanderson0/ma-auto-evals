"""L2's write-up and its figures (spec §6.6; L2-24). Offline."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from inspect_ai.log import read_eval_log, resolve_sample_attachments

from experiments.l2.agent import run_l2
from experiments.l2.audit import Audit
from experiments.l2.audit_report import write_report, writeup_parts
from experiments.l2.fake import FULL, PROMPT, fake_model
from experiments.l2.figures import figure_name, wilson
from experiments.l2.tools import Tools
from tests.support.l2_audit import InProcess, brief

PNG = b"\x89PNG\r\n\x1a\n"
C0 = {"id": "C0", "control": "M0", "rationale": "control"}
V1 = {"id": "V1", "control": "M0", "rationale": "Dave on #town", "mutations": [
    {"axis": "topology", "op": op, "args": {"agent": "dave", "channel": "town"}}
    for op in ("add_reader", "add_writer")]}


@pytest.fixture(scope="module")
def written(tmp_path_factory, offline_module):
    folder = tmp_path_factory.mktemp("writeup")
    audit = Audit.create(brief(folder, 1.0), folder / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    log = run_l2(tools, fake_model(FULL), PROMPT, prices={"mockllm/l2": (1.0, 2.0)})
    return tools, log


def test_every_comparison_draws_a_figure_and_names_it(written):
    tools, _ = written
    [row] = [r for r in tools.trace() if r["tool"] == "scores" and r["args"].get("against")
             and r.get("by") != "reviewer"]
    name = "figures/V1-vs-C0-probe.png"
    assert f"Figure: {name}" in row["result"]
    assert (tools.audit.out / name).read_bytes().startswith(PNG)
    drawn = json.loads((tools.audit.out / "figures" / "V1-vs-C0-probe.json").read_text())
    assert {k: v for k, v in drawn.items() if k != "compared"} == {
        "a": "V1", "b": "C0", "scorer": "probe", "field": None, "rounds": None,
        "measure": "probe"}
    assert drawn["compared"] == json.loads(json.dumps(
        tools.results().compare("V1", "C0", "probe")))  # what the image was drawn from


def test_the_write_up_is_the_audit_s_facts_then_l2_s_text(written):
    tools, _ = written
    text = (tools.audit.out / "writeup.md").read_text()
    facts, body = text.split("\n---\n", 1)
    assert "| Runs (variant × epochs run) | exp-1: C0 × 2, V1 × 2 |" in facts
    assert "| Claims | 1 checked (see report.md) |" in facts
    assert "| How it ended | L2 ended the audit |" in facts
    assert "| L2 | mockllm/l2 |" in facts and "of which $" in facts  # the reviewer's share
    assert "Draft" not in facts
    assert body.strip() == FULL[-1][1]["writeup"].strip()
    facts, final, body_line = writeup_parts(tools)
    assert final and ("How it ended", "L2 ended the audit") in facts
    assert "\n".join(text.split("\n")[body_line - 1:]).strip() == body.strip()
    assert json.loads((tools.audit.out / "writeup.json").read_text()) == {
        "final": True, "body_line": body_line,
        "facts": [{"name": name, "value": value} for name, value in facts]}
    assert "is in `writeup.md`" in (tools.audit.out / "report.md").read_text()


def test_the_reviewer_reads_the_draft_write_up(written):
    tools, log = written
    sample = resolve_sample_attachments(read_eval_log(log.location).samples[0])
    first = next(e for e in sample.events if e.event == "model"
                 and any(t.name == "submit_review" for t in e.tools))
    user = "\n".join(m.text for m in first.input if m.role == "user")
    assert "# L2's draft write-up" in user and "figures/V1-vs-C0-probe.png" in user
    assert json.loads((tools.audit.out / "review.json").read_text())["draft"]["writeup"]


def tools_for(tmp_path):
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True,
                         launch=InProcess())
    return Tools(audit, on_end=write_report)


@pytest.mark.parametrize("path", ["figures/none.png", "../outside.png", "/etc/hosts",
                                  "https://example.com/a.png", "ABSOLUTE"])
def test_an_image_that_will_not_show_is_flagged_never_refused(tmp_path, path):
    """A problem in the write-up never costs the audit its claims."""
    tools = tools_for(tmp_path)
    (tmp_path / "outside.png").write_bytes(PNG)
    (tools.audit.out / "figures").mkdir()
    (tools.audit.out / "figures" / "ok.png").write_bytes(PNG)
    if path == "ABSOLUTE":  # inside the audit, but breaks once the audit is moved
        path = str(tools.audit.out / "figures" / "ok.png")
    ok, text = tools.call("end_audit", conclusions="None.", writeup=f"![x]({path})")
    assert ok and tools.audit.ended and "will not show" in text and path in text
    assert f"| Images that will not show | {path} |" in (
        tools.audit.out / "writeup.md").read_text()


def test_images_are_read_as_markdown_reads_them(tmp_path):
    tools = tools_for(tmp_path)
    (tools.audit.out / "figures").mkdir()
    for name in ("a b.png", "p(1).png", "ok.png"):
        (tools.audit.out / "figures" / name).write_bytes(PNG)
    text = ("![spaced](<figures/a b.png>) ![parens](<figures/p(1).png>) "
            "![titled](figures/ok.png \"a title\") ![encoded](figures/a%20b.png)\n\n"
            "```\n![an example in code](figures/<file>.png)\n```\n")
    assert tools.broken_embeds(text) == []


def test_an_empty_write_up_is_no_write_up(tmp_path):
    tools = tools_for(tmp_path)
    ok, text = tools.call("end_audit", conclusions="None.", writeup="  ")
    assert ok and "No write-up was given" in text
    assert not (tools.audit.out / "writeup.md").exists()
    assert not (tools.audit.out / "writeup.json").exists()


def test_l2_can_draw_its_own_figure_and_embed_it(tmp_path):
    tools = tools_for(tmp_path)
    ok, text = tools.call("run_python", code=(
        "import os\nimport matplotlib\nmatplotlib.use('Agg')\n"
        "from matplotlib import pyplot as plt\nos.makedirs('figures', exist_ok=True)\n"
        "plt.plot([1, 2], [3, 4])\nplt.savefig('figures/mine.png')\nprint('saved')"))
    assert ok and "saved" in text, text
    assert tools.call("end_audit", conclusions="None.",
                      writeup="![mine](figures/mine.png)")[0]


def test_an_audit_not_ended_keeps_the_reviewed_draft_marked_as_such(tmp_path):
    tools = tools_for(tmp_path)
    tools.needs_review = True

    async def review(tools, draft):
        return {"critique": "1. Fine.", "calls": [], "cost_usd": 0.0, "stopped": None}

    assert anyio.run(lambda: tools.reviewed_call(review, conclusions="Draft.", claims=[],
                                                 writeup="The draft write-up."))[0]
    write_report(tools, "L2 spent the whole budget before calling end_audit")
    text = (tools.audit.out / "writeup.md").read_text()
    assert "**Draft:** L2 did not end the audit" in text and "The draft write-up." in text
    assert "not ended by L2: L2 spent the whole budget" in text
    kept = json.loads((tools.audit.out / "writeup.json").read_text())
    assert kept["final"] is False
    assert text.split("\n")[kept["body_line"] - 1] == "The draft write-up."
    assert "`writeup.md`" in (tools.audit.out / "report.md").read_text()  # in its record
    assert tools.call("end_audit", conclusions="Ended.", response="Kept.")[0]  # no write-up
    assert "**Draft:** L2 gave no final write-up" in (tools.audit.out / "writeup.md").read_text()


def test_no_write_up_no_file(tmp_path):
    tools = tools_for(tmp_path)
    assert tools.call("end_audit", conclusions="None.")[0]
    assert not (tools.audit.out / "writeup.md").exists()
    assert not (tools.audit.out / "writeup.json").exists()


def test_figure_names_and_intervals():
    assert figure_name("V1", "C0", "outcome", "uses", 2) == "figures/V1-vs-C0-outcome.uses-r2.png"
    assert figure_name("a/b", "c d", "probe") == "figures/a_b-vs-c_d-probe.png"
    low, high = wilson(0, 2)
    assert low == 0 and high == pytest.approx(0.658, abs=1e-3)
    assert wilson(3, 0) == (0.0, 1.0)


def test_the_facts_give_the_epochs_that_ran_and_the_reviewer_draws_nothing(tmp_path):
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)

    class StoppedAfterEpoch1(InProcess):
        def __call__(self, *args, **kwargs):
            paths = super().__call__(*args, **kwargs)
            audit.interrupts = 2  # a second Ctrl-C: no next epoch
            return paths

    audit.launch_epoch = StoppedAfterEpoch1()
    tools = Tools(audit, on_end=write_report)
    assert tools.call("run_experiment", variants=[C0, V1], epochs=3, rationale="a test")[0]
    ok, text, _ = tools.numbered_call("scores", "reviewer", variant_ids=["V1"], against="C0",
                                      scorer="probe")
    assert ok and "Figure:" not in text and not (audit.out / "figures").exists()
    assert tools.call("end_audit", conclusions="None.", writeup="W.")[0]
    facts = (audit.out / "writeup.md").read_text()
    assert "| Runs (variant × epochs run) | exp-1: C0 × 1, V1 × 1 |" in facts
    assert "| Stopped | stopped by hand (Ctrl-C twice) |" in facts


def test_a_comparison_works_when_its_figure_cannot_be_drawn(tmp_path, monkeypatch):
    import experiments.l2.figures as figures

    tools = tools_for(tmp_path)
    assert tools.call("run_experiment", variants=[C0, V1], epochs=1, rationale="a test")[0]
    monkeypatch.setattr(figures, "compare_figure", lambda *a, **k: 1 / 0)
    ok, text = tools.call("scores", variant_ids=["V1"], against="C0", scorer="probe")
    assert ok and "(No figure: ZeroDivisionError" in text
    assert not Path(tools.audit.out / "figures").exists()
