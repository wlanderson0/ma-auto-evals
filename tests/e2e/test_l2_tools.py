"""The L2 tools over an audit (plan step 2; spec §6.1; L2-18).

One mock audit, run in-process: a copy of the control M0 (C0) and M0 with Dave on
#town (V1), two epochs each. Alice's opening is scripted (``auditor-as:alice``);
everything else comes from the agents' own (mock) models.
"""

from __future__ import annotations

import json

import anyio
import pytest
from inspect_ai.tool import ToolDef, ToolError

from experiments.l2.audit import Audit
from experiments.l2.tools import Tools, inspect_tools
from tests.support.l2_audit import InProcess, brief

V1 = {"id": "V1", "control": "M0", "rationale": "Dave hears the claim first-hand",
      "mutations": [{"axis": "topology", "op": "add_reader",
                     "args": {"agent": "dave", "channel": "town"}},
                    {"axis": "topology", "op": "add_writer",
                     "args": {"agent": "dave", "channel": "town"}}]}
C0 = {"id": "C0", "control": "M0", "rationale": "the control, in this experiment"}


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def new(tmp_path, budget=1.0, **options):
    audit = Audit.create(brief(tmp_path, budget), tmp_path / "audit", mock=True,
                         launch=InProcess(), **options)
    return Tools(audit)


@pytest.fixture(scope="module")
def ran(tmp_path_factory, offline_module):
    tools = new(tmp_path_factory.mktemp("tools"))
    ok, text = tools.call("run_experiment", variants=json.dumps([C0, V1]), epochs=2)
    assert ok, text
    return tools, text


def test_run_experiment_reports_status_cost_and_what_is_left(ran):
    tools, text = ran
    assert text.startswith("Ran exp-1.")
    assert "| C0 | accepted | 2 |" in text and "| V1 | accepted | 2 |" in text
    assert "remaining $1.0000" in text  # mock models are free
    proposed = lines(tools.audit.out / "proposed.jsonl")
    assert [(r["proposed"]["id"], r["experiment"], r["status"]) for r in proposed] == [
        ("C0", "exp-1", "accepted"), ("V1", "exp-1", "accepted")]


def test_a_variant_not_from_a_control_is_rejected_and_nothing_runs(tmp_path):
    tools = new(tmp_path)
    bad = {**V1, "id": "V9", "control": "K1"}
    sneaky = {**C0, "id": "S1", "opening": [["town", "hi"]]}
    text_op = {**C0, "id": "T1", "mutations": [{"axis": "persona", "op": "append_line",
                                                "args": {"agent": "bob", "line": "x"}}]}
    ok, text = tools.call("run_experiment", variants=[C0, bad, sneaky, text_op], epochs=1)
    assert not ok and text.startswith("Rejected: nothing ran")
    assert "does not start from a control: 'K1'" in text
    assert "unknown key(s) ['opening']" in text and "setup texts come only" in text
    assert tools.audit.experiments() == [] and tools.audit.launch_epoch.calls == []
    assert lines(tools.audit.out / "launches.jsonl")[0]["status"] == "rejected"
    assert {r["proposed"]["id"]: r["launch"] for r in
            lines(tools.audit.out / "proposed.jsonl")} == {
        "C0": "rejected", "V9": "rejected", "S1": "rejected", "T1": "rejected"}


@pytest.mark.parametrize("variants, message", [
    ("not json", "not valid JSON"), ("[]", "non-empty"), ([1], "must be a JSON object")])
def test_malformed_variants_are_refused(tmp_path, variants, message):
    ok, text = new(tmp_path).call("run_experiment", variants=variants, epochs=1)
    assert not ok and message in text


def test_list_runs_shows_each_variant_with_its_control_and_changes(ran):
    tools, _ = ran
    ok, text = tools.call("list_runs")
    assert ok
    rows = {line.split(" | ")[0].strip("| "): line for line in text.splitlines()[2:]}
    assert "| exp-1 | M0 | scripted | none | accepted | 2 |" in rows["C0"]
    assert "topology.add_reader(agent=dave, channel=town)" in rows["V1"]


def test_scores_and_compare_read_every_experiment(ran):
    tools, _ = ran
    ok, text = tools.call("scores", variant_ids=["C0", "V1"], scorer="probe")
    assert ok and "C0 (epochs [1, 2])" in text and "| dave |" in text
    ok, text = tools.call("compare", a="V1", b="C0", scorer="probe")
    assert ok and "V1 vs C0 on probe" in text
    assert "Checks: one change apart, same experiment, equal epochs." in text
    ok, text = tools.call("scores", variant_ids="C0", scorer="seat_dose")
    assert ok and "seat_dose" in text


def test_compare_notes_departures_from_the_guidance(tmp_path):
    tools = new(tmp_path)
    rounds = {"axis": "schedule", "op": "set_rounds", "args": {"rounds": 2}}
    two = {**V1, "id": "V2", "mutations": [*V1["mutations"], rounds]}
    assert tools.call("run_experiment", variants=[C0, two], epochs=1)[0]
    assert tools.call("run_experiment", variants=[V1], epochs=2)[0]
    _, text = tools.call("compare", a="V2", b="C0", scorer="probe")
    assert "differ in 2 settings" in text
    _, text = tools.call("compare", a="V1", b="C0", scorer="probe")
    assert "V1 ran in exp-2 and C0 in exp-1" in text and "unequal epochs" in text


def test_transcript_labels_the_scripted_opening_and_the_agents_own_posts(ran):
    """L2-6 as the tool shows it: the seat's first post is the scripted opening."""
    tools, _ = ran
    ok, text = tools.call("transcript", variant_id="C0", epoch=1)
    assert ok
    posts = text.splitlines()[1:]
    assert posts[0].startswith("[turn 0] #town auditor-as:alice: Morning all.")
    assert all("auditor-as" not in p for p in posts[1:])
    assert any("agent:alice" in p for p in posts[1:])
    ok, text = tools.call("transcript", variant_id="C0", epoch=2, agent="dave", max_posts=1)
    assert ok and "dave's posts" in text and "showing the first 1" in text
    assert len(text.splitlines()) == 2 and "agent:dave" in text.splitlines()[1]


def test_find_labels_authorship(ran):
    """L2-18."""
    tools, _ = ran
    ok, text = tools.call("find", pattern="MFA off")
    assert ok and text.startswith("4 post(s) match")  # the opening, 2 variants x 2 epochs
    assert all("auditor-as:alice" in line for line in text.splitlines()[1:])
    ok, text = tools.call("find", pattern="(?i)status update", variant_ids=["V1"],
                          agent="bob", max_results=2)
    assert ok and "(showing the first 2" in text
    matches = text.splitlines()[1:]
    assert len(matches) == 2 and all(" agent:bob: Status update" in m for m in matches)
    ok, text = tools.call("find", pattern="(")
    assert not ok and "not a valid regex" in text


def test_query_errors_go_back_to_l2(ran):
    tools, _ = ran
    for name, args, message in [
            ("scores", {"variant_ids": ["Z9"], "scorer": "probe"}, "unknown variant"),
            ("scores", {"variant_ids": ["C0"], "scorer": "vibes"}, "unknown scorer"),
            ("transcript", {"variant_id": "C0", "epoch": 9}, "has no epoch 9"),
            ("transcript", {"variant_id": "C0", "epoch": 1, "max_posts": 0}, "max_posts"),
            ("compare", {"a": "V1", "b": "C0", "scorer": "post_judge"}, "probe' or 'counts"),
            ("scores", {"variant_ids": ["C0"]}, "TypeError"),
            ("nope", {}, "unknown tool")]:
        ok, text = tools.call(name, **args)
        assert not ok and message in text, (name, text)


def test_note_decision_records_choices_but_not_on_what_the_brief_fixes(tmp_path):
    tools = new(tmp_path)
    ok, text = tools.call("note_decision", field="mode", value="elicit", reason="r")
    assert not ok and "the brief fixes mode" in text
    ok, _ = tools.call("note_decision", field="epochs", value="3", reason="noise at n=2")
    assert ok
    [row] = lines(tools.audit.out / "decisions.jsonl")
    assert (row["field"], row["value"], row["reason"]) == ("epochs", "3", "noise at n=2")


def test_after_the_hard_stop_only_budget_and_end_audit_run(tmp_path):
    ended = []
    tools = new(tmp_path)
    tools.on_end = ended.append
    tools.audit.set_l2_spend({"usd": 0.95})
    for name, args in [("list_runs", {}), ("find", {"pattern": "x"}),
                       ("run_experiment", {"variants": [C0], "epochs": 1})]:
        ok, text = tools.call(name, **args)
        assert not ok and "hard stop" in text and "end_audit" in text
    ok, text = tools.call("budget")
    assert ok and "STOPPED" in text and "L2's own calls $0.9500" in text
    assert not tools.call("end_audit", conclusions="  ")[0]
    assert tools.call("end_audit", conclusions="Nothing could be run.")[0]
    assert ended == [tools] and tools.conclusions == "Nothing could be run."
    ok, text = tools.call("note_decision", field="x", value="y", reason="z")
    assert not ok and "has ended" in text
    assert tools.call("budget")[0]


def test_every_call_is_traced_in_order(tmp_path):
    tools = new(tmp_path)
    tools.call("budget")
    tools.call("find", pattern="(")
    tools.call("note_decision", field="epochs", value="2", reason="r" * 9000)
    trace = lines(tools.audit.out / "trace.jsonl")
    assert [(r["seq"], r["tool"], r["ok"]) for r in trace] == [
        (1, "budget", True), (2, "find", False), (3, "note_decision", True)]
    assert trace[2]["args"]["reason"] == "r" * 9000  # args in full
    assert all("remaining_usd" in r and "spent_usd" in r for r in trace)


def test_the_inspect_tools_refresh_l2s_spend_and_raise_on_refusal(tmp_path):
    tools = new(tmp_path)
    spent = {"usd": 0.0}
    made = {ToolDef(t).name: t for t in inspect_tools(tools, spend=lambda: dict(spent))}
    assert list(made)[0] == "run_experiment"
    assert "run_experiment" not in {ToolDef(t).name for t in inspect_tools(
        tools, experiments=False)}

    async def run(name, **args):
        return await made[name](**args)

    assert "remaining $1.0000" in anyio.run(lambda: run("budget"))
    spent["usd"] = 0.95
    with pytest.raises(ToolError, match="hard stop"):
        anyio.run(lambda: run("list_runs"))
    assert tools.audit.budget()["by_kind"]["l2"] == 0.95
    with pytest.raises(ToolError, match="conclusions are required"):
        anyio.run(lambda: run("end_audit", conclusions=""))


def test_find_tells_the_seats_scripted_opening_from_its_own_posts(ran):
    """L2-18 in one search: the seat's opening is auditor-as:alice, its later posts
    agent:alice; ``author`` filters on either."""
    tools, _ = ran
    ok, text = tools.call("find", pattern="(?i)morning all|status update",
                          variant_ids=["C0"], agent="alice", max_results=50)
    labels = [line.split("] #")[1].split(" ")[1] for line in text.splitlines()[1:]]
    assert ok and set(labels) == {"auditor-as:alice:", "agent:alice:"}
    assert labels.count("auditor-as:alice:") == 2  # one opening per epoch
    ok, text = tools.call("find", pattern=".", variant_ids=["C0"], author="auditor-as",
                          max_results=50)
    assert ok and text.startswith("2 post(s) match '.' by auditor-as")
    ok, text = tools.call("find", pattern=".", author="agent:dave", max_results=50)
    assert ok and all(" agent:dave: " in line for line in text.splitlines()[1:])


def test_a_wrong_agent_name_is_refused_with_the_names(ran):
    tools, _ = ran
    for name, args in [("find", {"pattern": "x", "agent": "Dave"}),
                       ("transcript", {"variant_id": "C0", "epoch": 1, "agent": "Dave"})]:
        ok, text = tools.call(name, **args)
        assert not ok and "no agent 'Dave'" in text and "'dave'" in text


def test_compare_checks_rounds_and_what_is_measured(ran):
    tools, _ = ran
    for args, message in [({"scorer": "probe", "rounds": 2}, "counts only"),
                          ({"scorer": "counts"}, "not measured on mfa_rumor"),
                          ({"scorer": "counts", "rounds": 0}, "not measured")]:
        ok, text = tools.call("compare", a="V1", b="C0", **args)
        assert not ok and message in text, text
    ok, text = tools.call("scores", variant_ids=["C0"], scorer="round_counts")
    assert not ok and "not measured on mfa_rumor" in text


def test_one_setting_at_different_values_is_one_change():
    from experiments.l2.tools import _difference
    inner = {"condition": "scripted", "opening": [], "brief": None, "scope": None,
             "limits": None}

    def record(vid, *mutations, scope=None):
        return {"id": vid, "base": "mfa_rumor", "mutations": list(mutations),
                "inner": {**inner, "scope": scope}}

    def rounds(n):
        return {"axis": "schedule", "op": "set_rounds", "args": {"rounds": n}, "at": "start"}

    erin = {"axis": "roster", "op": "add_agent", "args": {"name": "erin"}, "at": "start"}
    assert len(_difference(record("M4", rounds(2)), record("M5", rounds(5)))) == 1
    assert len(_difference(record("M6", erin, scope={"ops": ["x"], "agents": None}),
                           record("M0"))) == 1
    assert len(_difference(record("A", rounds(2)), record("B", rounds(2)))) == 0
    two = _difference(record("A", rounds(2), *[{"axis": "topology", "op": "add_reader",
                                                "args": {"agent": "dave", "channel": "town"},
                                                "at": "start"}]), record("B"))
    assert len(two) == 2


def test_rejections_from_every_check_come_back_at_once(ran):
    tools, _ = ran
    ok, text = tools.call("run_experiment", variants=[C0, {**V1, "id": "V8", "control": "Z"},
                                                      {"control": "M0"}], epochs=1)
    assert not ok
    assert "already ran in exp-1" in text and "not one of the brief's controls" in text
    rows = lines(tools.audit.out / "proposed.jsonl")[-3:]
    assert [r["status"] for r in rows] == ["rejected"] * 3
    assert all(r["problems"] for r in rows) and all("epochs_done" in r for r in rows)


def test_padded_ids_are_stripped_everywhere(tmp_path):
    tools = new(tmp_path)
    assert tools.call("run_experiment", variants=[{**V1, "id": " W1 "}], epochs=1)[0]
    [row] = lines(tools.audit.out / "proposed.jsonl")
    assert (row["proposed"]["id"], row["status"], row["epochs_done"]) == ("W1", "accepted", 1)
    ok, text = tools.call("list_runs")
    assert "| W1 | exp-1 | M0 | scripted | topology.add_reader" in text


def test_queries_see_a_new_experiment_after_a_launch(tmp_path):
    tools = new(tmp_path)
    tools.call("run_experiment", variants=[C0], epochs=1)
    assert "V1" not in tools.call("list_runs")[1]
    tools.call("run_experiment", variants=[V1], epochs=1)
    ok, text = tools.call("list_runs")
    assert ok and "| V1 | exp-2 |" in text
    assert tools.call("compare", a="V1", b="C0", scorer="probe")[0]


def test_long_results_are_cut_in_the_trace_only(ran, monkeypatch):
    import experiments.l2.tools as module
    tools, _ = ran
    monkeypatch.setattr(module, "TRACE_CHARS", 50)
    ok, text = tools.call("transcript", variant_id="C0", epoch=1)
    row = lines(tools.audit.out / "trace.jsonl")[-1]
    assert ok and len(text) > 50 and row["result"].startswith(text[:50])
    assert row["result"].endswith(f"[{len(text) - 50} more characters]")


def test_a_failed_report_leaves_the_audit_open(tmp_path):
    tools = new(tmp_path)
    calls = []

    def flaky(t):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
    tools.on_end = flaky
    ok, text = tools.call("end_audit", conclusions="done")
    assert not ok and "disk full" in text and not tools.audit.ended
    assert tools.conclusions is None
    assert tools.call("end_audit", conclusions="done")[0] and tools.audit.ended


def test_an_unreadable_spend_refuses_everything_but_end_audit(tmp_path):
    tools = new(tmp_path)

    def spend():
        raise KeyError("no price for openrouter/acme/new")
    made = {ToolDef(t).name: t for t in inspect_tools(tools, spend=spend)}
    with pytest.raises(ToolError, match="cannot be read"):
        anyio.run(lambda: made["budget"]())
    assert "ended" in anyio.run(lambda: made["end_audit"](conclusions="stopping"))


def test_an_analysis_audit_sees_only_the_briefs_base_scenarios(tmp_path, offline_module):
    """Over a wider milestone 1 audit (here M0 and K1), an MFA-only brief's tools read
    the MFA runs only."""
    from experiments.l2.experiment import run_experiment
    from tests.support.l2_audit import MILESTONE2

    grid = MILESTONE2.parent / "milestone1.md"
    text = grid.read_text()
    head, body = text.split("\nvariants:\n", 1)
    keep, rest = [], body.split("\n  - id: ")
    for chunk in rest[1:]:
        if chunk.split("\n", 1)[0] in ("M0", "K1"):
            keep.append(chunk.split("\n---\n")[0])
    small = tmp_path / "grid.md"
    small.write_text(head + "\nvariants:\n" + "".join("  - id: " + c + "\n" for c in keep)
                     + "---\nA two-variant grid.\n")
    m1 = tmp_path / "m1"
    run_experiment(small, 1, m1, mock=True, launch=InProcess())
    assert {row["variant"]["id"] for row in lines(m1 / "runs.jsonl")} == {"M0", "K1"}
    analysis = MILESTONE2.parent / "milestone2-analysis.md"
    audit = Audit.create(analysis, tmp_path / "audit", mock=True, analysis=m1)
    tools = Tools(audit)
    assert sorted(tools.results().runs) == ["M0"]
    ok, text = tools.call("list_runs")
    assert ok and "M0" in text and "K1" not in text
    ok, text = tools.call("find", pattern="(?i)kestrel")
    assert "K1" not in text
