"""The L2 brief parser and the milestone 1 brief (L2 Task 2; spec §2–§4)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from experiments.l2.brief import BriefError, parse_brief
from experiments.l2.model import InnerSpec, L2Brief, Mutation, Problems
from experiments.scenarios import kestrel_dose
from orbit_audit.branches import AuditLimits
from orbit_audit.scope import AuditScope

MILESTONE1 = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / "milestone1.md"
KESTREL = re.compile(r"\bkestrel\b", re.IGNORECASE)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "brief.md"
    path.write_text(text)
    return path


def test_minimal_brief_leaves_everything_else_to_l2(tmp_path):
    brief = parse_brief(write(tmp_path, "---\nbudget_usd: 40\n---\nHow does spread "
                                        "depend on topology?\n"))
    assert brief == L2Brief(question="How does spread depend on topology?", budget_usd=40.0)
    assert all(getattr(brief, f) is None for f in (
        "mode", "base_scenarios", "base_logs", "measure", "allowed_changes", "variants",
        "hypotheses", "inner"))


@pytest.mark.parametrize("text, message", [
    ("---\nmode: elicit\n---\nQ?", "budget_usd is required"),
    ("Q? with no header", "budget_usd is required"),
    ("---\nbudget_usd: 5\nbudget: 3\n---\nQ?", "unknown key(s) ['budget']"),
    ("---\nbudget_usd: 0\n---\nQ?", "positive number"),
    ("---\nbudget_usd: true\n---\nQ?", "positive number"),
    ("---\nbudget_usd: 5\n---\n   \n", "question"),
    ("---\nbudget_usd: 5\nmode: explore\n---\nQ?", "mode must be one of"),
    ("---\nbudget_usd: 5\nbase_scenarios: [nope]\n---\nQ?", "unknown base_scenarios"),
    ("---\nbudget_usd: 5\nallowed_changes: [colour]\n---\nQ?", "unknown allowed_changes"),
    ("---\nbudget_usd: 5\ninner: {condition: l3}\n---\nQ?", "condition must be one of"),
    ("---\nbudget_usd: 5\ninner: {condition: l1, budget: 3}\n---\nQ?", "unknown key(s)"),
    ("---\nbudget_usd: 5\n: [\n---\nQ?", "YAML"),
    ("---\nbudget_usd: .nan\n---\nQ?", "positive number"),
    ("---\nbudget_usd: .inf\n---\nQ?", "positive number"),
    ("---\nbudget_usd: 5\nbudget_usd: 500\n---\nQ?", "duplicate key 'budget_usd'"),
    ("---\nbudget_usd: 5\nmode: elicit\n", "not closed"),
    ("---\nbudget_usd: 5\n---extra\nQ?", "not closed"),
    ("---\nbudget_usd: 5\n1: a\nfoo: b\n---\nQ?", "unknown key(s) [1, 'foo']"),
    ("---\nbudget_usd: 5\nvariants: []\n---\nQ?", "non-empty list"),
])
def test_bad_briefs_fail_with_a_clear_message(tmp_path, text, message):
    with pytest.raises(BriefError, match=re.escape(message)):
        parse_brief(write(tmp_path, text))


VARIANT_BRIEF = """\
---
budget_usd: 12.5
mode: characterise
base_scenarios: [mfa_rumor]
allowed_changes: [topology, schedule, probes]
hypotheses: [the bridge matters]
inner: {condition: scripted, limits: {max_branches: 1}}
x-notes: ignored, for YAML anchors
variants:
  - id: A
    base: mfa_rumor
    rationale: why
    mutations:
      - {axis: topology, op: add_writer, args: {agent: dave, channel: town}}
      - {axis: schedule, op: activate_agent, args: {agent: erin}, at: "after_round:1"}
    inner:
      opening: [[town, "  hello  "]]
      scope: {ops: [replace_generation, amend_pending], agents: null}
  - id: B
    base: mfa_rumor
    rationale: why not
    inner: {condition: l1, brief: be subtle}
---
Question?
"""


def test_variants_and_inner_specs_parse(tmp_path):
    brief = parse_brief(write(tmp_path, VARIANT_BRIEF))
    a, b = brief.variants
    assert (brief.mode, brief.base_scenarios, brief.hypotheses) == (
        "characterise", ("mfa_rumor",), ("the bridge matters",))
    assert brief.inner == InnerSpec(condition="scripted", limits=AuditLimits(max_branches=1))
    assert a.mutations == (
        Mutation("topology", "add_writer", {"agent": "dave", "channel": "town"}),
        Mutation("schedule", "activate_agent", {"agent": "erin"}, at="after_round:1"))
    # Variant fields are merged over the brief's inner, field by field.
    assert a.inner == InnerSpec(
        condition="scripted", opening=(("town", "hello"),),
        scope=AuditScope(ops={"replace_generation", "amend_pending"}, agents=None),
        limits=AuditLimits(max_branches=1))
    assert b.inner == InnerSpec(condition="l1", brief="be subtle",
                                limits=AuditLimits(max_branches=1))
    assert b.mutations == ()


@pytest.mark.parametrize("variant, message", [
    ("{id: A, base: mfa_rumor}", "rationale is required"),
    ("{id: 'A B', base: mfa_rumor, rationale: r}", "id may only use"),
    ("{id: A, base: mfa_rumor, rationale: r, mutations: [{axis: colour, op: x}]}",
     "unknown axis"),
    ("{id: A, base: mfa_rumor, rationale: r, mutations: [{axis: roster, op: x, when: 1}]}",
     "unknown key(s) ['when']"),
    ("{id: A, base: mfa_rumor, rationale: r, inner: {opening: [[town]]}}",
     "is not a [channel, text] pair"),
    ("{id: A, base: mfa_rumor, rationale: r, inner: {scope: {ops: [teleport], agents: null}}}",
     "invalid scope"),
    ("{id: A, base: mfa_rumor, rationale: r, inner: {scope: {ops: null, agents: alice}}}",
     "scope agents must be a list"),
    ("{id: A, base: mfa_rumor, rationale: r, inner: {scope: {agents: [alice]}}}",
     "scope needs ['ops']"),
    ("{id: A, base: mfa_rumour, rationale: r}", "unknown base 'mfa_rumour'"),
    ("{id: A, base: release_pressure, rationale: r}", "not in base_scenarios"),
    ("{id: A, base: mfa_rumor, rationale: r, mutations: {}}", "mutations must be a list"),
    ("{id: A, base: mfa_rumor, rationale: r, mutations: [{axis: roster, op: x, args: []}]}",
     "args must be a mapping"),
    ("{id: A, base: mfa_rumor, rationale: r, inner: {condition: l1, opening: [[town, hi]]}}",
     "only played by the scripted condition"),
    ("{id: A, base: mfa_rumor, rationale: r, inner: {limits: {max_branches: true}}}",
     "whole numbers"),
    ("{id: A, base: mfa_rumor, rationale: r, inner: {limits: {max_branches: '5'}}}",
     "whole numbers"),
    ("{id: A, base: mfa_rumor, rationale: r, inner: {limits: {max_branches: -3}}}",
     "whole numbers"),
    ("{id: A, base: mfa_rumor, rationale: r, inner: {limits: {max_wakes: 3}}}",
     "invalid limits"),
])
def test_bad_variants_fail_with_a_clear_message(tmp_path, variant, message):
    text = (f"---\nbudget_usd: 1\nbase_scenarios: [mfa_rumor, kestrel_naming]\n"
            f"inner: {{condition: scripted}}\nvariants: [{variant}]\n---\nQ?")
    with pytest.raises(BriefError, match=re.escape(message)):
        parse_brief(write(tmp_path, text))


@pytest.mark.parametrize("second", ["A", "a"])  # ids name log folders: case-insensitive
def test_duplicate_ids_are_rejected(tmp_path, second):
    twice = "---\nbudget_usd: 1\ninner: {condition: scripted}\nvariants: [{id: A, base: " \
            f"mfa_rumor, rationale: r}}, {{id: {second}, base: mfa_rumor, rationale: r}}]\n---\nQ?"
    with pytest.raises(BriefError, match="duplicate id"):
        parse_brief(write(tmp_path, twice))


def test_a_byte_order_mark_and_padded_channels_are_tolerated(tmp_path):
    path = tmp_path / "bom.md"
    path.write_text("---\nbudget_usd: 2\ninner: {condition: scripted}\nvariants: [{id: A, "
                    "base: mfa_rumor, rationale: r, inner: {opening: [[' town ', hi]]}}]\n"
                    "---\nQ?", encoding="utf-8-sig")
    [variant] = parse_brief(path).variants
    assert variant.inner.opening == (("town", "hi"),)


def test_anchored_args_are_not_shared_between_variants(tmp_path):
    text = ("---\nbudget_usd: 1\ninner: {condition: scripted}\nx-a: &m {axis: roster, op: "
            "add_agent, args: {channels: {read: [town]}}}\nvariants: [{id: A, base: mfa_rumor, "
            "rationale: r, mutations: [*m]}, {id: B, base: mfa_rumor, rationale: r, "
            "mutations: [*m]}]\n---\nQ?")
    a, b = parse_brief(write(tmp_path, text)).variants
    a.mutations[0].args["channels"]["read"].append("pod")
    assert b.mutations[0].args["channels"]["read"] == ["town"]


def test_missing_inner_is_rejected(tmp_path):
    bare = "---\nbudget_usd: 1\nvariants: [{id: A, base: mfa_rumor, rationale: r}]\n---\nQ?"
    with pytest.raises(BriefError, match="no inner spec"):
        parse_brief(write(tmp_path, bare))


def test_problems_default_to_none():
    assert Problems() == Problems((), ())
    assert not Problems(warnings=("w",)).rejected and Problems(rejections=("r",)).rejected


# ------------------------------------------------------------- milestone 1
@pytest.fixture(scope="module")
def grid() -> L2Brief:
    return parse_brief(MILESTONE1)


def test_milestone1_fixes_variants_measure_and_inner(grid):
    assert [v.id for v in grid.variants] == [f"M{i}" for i in range(8)] + [
        f"K{i}" for i in range(1, 8)]
    assert {v.base for v in grid.variants if v.id[0] == "M"} == {"mfa_rumor"}
    assert {v.base for v in grid.variants if v.id[0] == "K"} == {"kestrel_naming"}
    assert all(v.inner.condition == "scripted" for v in grid.variants)
    assert grid.inner == InnerSpec(condition="scripted")
    assert grid.mode == "characterise" and grid.measure
    assert grid.base_scenarios == ("mfa_rumor", "kestrel_naming")
    assert grid.budget_usd > 0
    assert all(v.rationale for v in grid.variants)


def test_milestone1_openings_are_one_message_on_town(grid):
    for variant in grid.variants:
        assert len(variant.inner.opening) == 1, variant.id
        assert variant.inner.opening[0][0] == "town", variant.id


def openings(grid, ids) -> set[str]:
    return {v.inner.opening[0][1] for v in grid.variants if v.id in ids}


def test_milestone1_openings_are_shared_within_a_scenario(grid):
    [mfa] = openings(grid, {f"M{i}" for i in range(8)})
    assert "MFA" in mfa and not KESTREL.search(mfa)
    [shared] = openings(grid, {"K1", "K3", "K4", "K5", "K6", "K7"})
    [tag] = openings(grid, {"K2"})
    assert tag == "[Kestrel] " + shared  # D5: the tag is part of the TAG dose


def test_milestone1_kestrel_openings_fit_their_doses(grid):
    [shared] = openings(grid, {"K1"})
    [tag] = openings(grid, {"K2"})
    assert len(KESTREL.findall(shared)) == 1
    assert kestrel_dose((shared, "Kestrel database step done.")) == "STEADY"
    assert kestrel_dose((tag,)) == "TAG"


def test_milestone1_doses_and_persona_lines(grid):
    by_id = {v.id: v for v in grid.variants}
    doses = {vid: [m.args.get("dose") for m in v.mutations if m.op == "append_line"]
             for vid, v in by_id.items()}
    assert "K0" not in doses and doses["K2"] == ["TAG"]
    assert all(doses[f"K{i}"] == ["STEADY"] for i in (1, 3, 4, 5, 6, 7))
    assert all(doses[f"M{i}"] == [None] for i in range(8))  # Alice's belief line
    lines = {m.args["line"] for v in grid.variants for m in v.mutations
             if m.op == "append_line"}
    assert all(line.endswith(".") and line[0].isupper() for line in lines)  # sentences
    assert {m.args["agent"] for v in grid.variants for m in v.mutations
            if m.op == "append_line"} == {"alice"}


def both(op: str, agent: str, channel: str) -> list[tuple[str, str, dict]]:
    """Read and write on ``channel`` for ``agent``, added or removed."""
    return [("topology", f"{op}_{role}", {"agent": agent, "channel": channel})
            for role in ("reader", "writer")]


ROUNDS = lambda n: [("schedule", "set_rounds", {"rounds": n})]  # noqa: E731
EXPECTED = {  # variant -> (control, its exact extra mutations as (axis, op, args, [at]))
    "M1": ("M0", both("add", "dave", "town")), "M2": ("M0", both("add", "alice", "pod")),
    "M3": ("M0", both("remove", "carol", "pod")), "M4": ("M0", ROUNDS(2)),
    "M5": ("M0", ROUNDS(5)),
    "K3": ("K1", both("add", "dave", "town")), "K4": ("K1", both("add", "alice", "pod")),
    "K5": ("K1", both("remove", "carol", "pod")), "K6": ("K1", ROUNDS(2)),
    "K7": ("K1", ROUNDS(5)),
}


def test_milestone1_each_variant_changes_one_thing_from_its_control(grid):
    by_id = {v.id: v for v in grid.variants}

    def extra(vid: str, control: str) -> list[Mutation]:
        assert all(m in by_id[vid].mutations for m in by_id[control].mutations), vid
        return [m for m in by_id[vid].mutations if m not in by_id[control].mutations]

    for vid, (control, mutations) in EXPECTED.items():
        assert extra(vid, control) == [Mutation(*m) for m in mutations], vid
        assert by_id[vid].inner == by_id[control].inner, vid
    [advocate] = extra("M6", "M0")
    assert (advocate.axis, advocate.op) == ("roster", "add_agent")
    assert set(advocate.args) == {"name", "persona", "extra", "model", "channels", "turns"}
    assert advocate.args["channels"] == {"read": ["town"], "write": ["town"]}
    assert advocate.args["turns"] is False and advocate.args["name"] == "erin"
    assert extra("M7", "M6") == [Mutation("schedule", "activate_agent",
                                          {"agent": "erin", "position": "after:alice"},
                                          at="after_round:1")]
    assert by_id["M7"].inner == by_id["M6"].inner  # same helper scope (L2-8)
    assert by_id["M6"].inner.scope == AuditScope(
        ops={"replace_generation", "amend_pending"}, agents=None)
    assert by_id["M6"].inner.opening == by_id["M0"].inner.opening
    assert all(by_id[v].inner.scope is None for v in by_id if v not in ("M6", "M7"))
    # K2 differs from K1 only in the dose line (and the tagged opening).
    assert [m.op for m in by_id["K2"].mutations] == ["append_line"]


MILESTONE2 = MILESTONE1.parent / "milestone2.md"


def test_milestone2_fixes_controls_not_variants_and_milestone1s_measure():
    brief = parse_brief(MILESTONE2)
    m1 = parse_brief(MILESTONE1)
    grid = {v.id: v for v in m1.variants}
    assert brief.variants is None and [c.id for c in brief.controls] == ["M0", "M6"]
    assert brief.base_scenarios == ("mfa_rumor",) and brief.measure == m1.measure
    for control in brief.controls:  # the same setup (and texts) as the grid's
        same = grid[control.id]
        assert (control.base, control.mutations, control.inner) == (
            same.base, same.mutations, same.inner)


def test_controls_parse_like_variants_and_cannot_share_their_ids(tmp_path):
    entry = ("  - {id: C1, base: mfa_rumor, rationale: r, mutations: [], "
             "inner: {condition: baseline}}\n")
    brief = parse_brief(write(tmp_path, "---\nbudget_usd: 1\ncontrols:\n" + entry
                              + "---\nQ?\n"))
    assert brief.controls[0].id == "C1" and brief.variants is None
    with pytest.raises(BriefError, match="name both a variant and a control"):
        parse_brief(write(tmp_path, "---\nbudget_usd: 1\ncontrols:\n" + entry
                          + "variants:\n" + entry + "---\nQ?\n"))
    with pytest.raises(BriefError, match=r"controls must be a non-empty list"):
        parse_brief(write(tmp_path, "---\nbudget_usd: 1\ncontrols: []\n---\nQ?\n"))
    with pytest.raises(BriefError, match=r"control C1: unknown base"):
        parse_brief(write(tmp_path, "---\nbudget_usd: 1\ncontrols:\n"
                          + entry.replace("mfa_rumor", "nope") + "---\nQ?\n"))
