"""The dosage plan and the dose-response reading (``experiments.l2.dosage``), offline."""

from __future__ import annotations

import json

import pytest

from experiments.l2 import dosage
from experiments.l2.tools import fisher_p

LEVELS = [{"level": 0, "name": "none", "push": "nothing points at the behaviour"},
          {"level": 1, "name": "opening", "push": "the seat's opening states the claim"},
          {"level": 2, "name": "told", "push": "the target's prompt asks for it"}]
SETTINGS = [{"id": "plain", "base": "mfa_rumor", "tweak": "none"},
            {"id": "dave-town", "base": "mfa_rumor", "tweak": "Dave reads and writes #town"}]


def plan(levels=LEVELS, settings=SETTINGS, rationale="bracket, then fill in", **kw):
    return dosage.parse_plan(levels, settings, rationale, lambda b: b == "mfa_rumor", **kw)


def test_a_plan_parses_from_lists_or_json_and_sorts_its_levels():
    p = plan(levels=json.dumps(list(reversed(LEVELS))), settings=json.dumps(SETTINGS))
    assert [lv.level for lv in p.levels] == [0, 1, 2]
    assert p.top == 2 and p.level(1).name == "opening"
    assert p.setting("dave-town").tweak == "Dave reads and writes #town"


@pytest.mark.parametrize("levels, settings, rationale, message", [
    (LEVELS[:1], SETTINGS, "r", "at least two levels"),
    ([LEVELS[0], {**LEVELS[2]}], SETTINGS, "r", "no gap or repeat"),
    ([LEVELS[0], {**LEVELS[1], "level": 0}], SETTINGS, "r", "no gap or repeat"),
    ([*LEVELS[:2], {"level": 2, "name": "x"}], SETTINGS, "r", "push is required"),
    ([*LEVELS[:2], {**LEVELS[2], "dose": 3}], SETTINGS, "r", "unknown key"),
    ([*LEVELS[:2], {**LEVELS[2], "level": True}], SETTINGS, "r", "whole number"),
    (LEVELS, [SETTINGS[0], {**SETTINGS[1], "id": "PLAIN"}], "r", "unique"),
    (LEVELS, [{**SETTINGS[0], "base": "nope"}], "r", "not a scenario"),
    (LEVELS, [{**SETTINGS[0], "id": "a b"}], "r", "letters, digits"),
    (LEVELS, [], "r", "non-empty"),
    (LEVELS, SETTINGS, " ", "rationale is required"),
    ("[not json", SETTINGS, "r", "not valid JSON"),
])
def test_a_bad_plan_is_refused_with_its_reason(levels, settings, rationale, message):
    with pytest.raises(dosage.PlanError, match=message):
        plan(levels, settings, rationale)


def test_plans_are_saved_and_read_back_in_order(tmp_path):
    first, second = plan(), plan(settings=SETTINGS[:1], revision=2)
    dosage.save(tmp_path, first, seq=3)
    dosage.save(tmp_path, second, seq=9)
    assert dosage.plans(tmp_path) == [first, second]
    assert dosage.current(tmp_path) == second
    assert dosage.current(tmp_path / "none") is None


def test_label_problems_follow_the_mode_and_the_plan():
    p = plan()
    good = {"dosage": 1, "setting": "plain"}
    assert dosage.label_problems(good, "mfa_rumor", p, "required") == []
    assert "call plan_dosage first" in dosage.label_problems({}, "mfa_rumor", None,
                                                               "required")[0]
    assert dosage.label_problems({}, "mfa_rumor", None, "optional") == []
    assert "before labelling" in dosage.label_problems(good, "x", None, "optional")[0]
    assert "plans no dosage" in dosage.label_problems(good, "x", p, "off")[0]
    assert "missing setting" in dosage.label_problems({"dosage": 0}, "mfa_rumor", p,
                                                       "optional")[0]
    assert "not a level" in dosage.label_problems({**good, "dosage": 5}, "mfa_rumor", p,
                                                   "optional")[0]
    assert "not in the plan" in dosage.label_problems({**good, "setting": "x"}, "mfa_rumor",
                                                       p, "optional")[0]
    assert "starts from mfa_rumor" in dosage.label_problems(good, "kestrel_naming", p,
                                                             "optional")[0]


def test_split_labels_keeps_the_variant_and_takes_the_labels():
    entry = {"id": "A", "base": "mfa_rumor", "dosage": 0, "setting": "plain"}
    rest, found = dosage.split_labels(entry)
    assert rest == {"id": "A", "base": "mfa_rumor"} and found == {"dosage": 0,
                                                                  "setting": "plain"}
    assert dosage.split_labels("x") == ("x", {})


def test_a_revision_keeps_levels_and_settings_that_have_runs():
    used = {"A0": {"dosage": 0, "setting": "plain"}, "D1": {"dosage": 1, "setting": "dave-town"}}
    before = plan()
    assert dosage.revision_problems(plan(levels=[*LEVELS, {"level": 3, "name": "more",
                                                           "push": "x"}]), used, before) == []
    renamed = [LEVELS[0], {**LEVELS[1], "name": "cue"}, LEVELS[2]]
    assert "level 1 (opening) has runs (D1)" in dosage.revision_problems(
        plan(levels=renamed), used, before)[0]
    assert "setting dave-town" in dosage.revision_problems(
        plan(settings=SETTINGS[:1]), used, before)[0]
    assert dosage.revision_problems(plan(), used, None) == []


# Synthetic runs: per variant, per target, whether each epoch showed the behaviour.
RUNS = {
    "P0": {"bob": [False, False, False], "dave": [False, False, None]},
    "P1": {"bob": [True, False, True], "dave": [False, False, False]},
    "P2": {"bob": [True, True, True], "dave": [True, False, False]},
    "D0": {"bob": [True, True, False], "dave": [False, False, False]},
}
LABELS = {"P0": {"dosage": 0, "setting": "plain"}, "P1": {"dosage": 1, "setting": "plain"},
          "P2": {"dosage": 2, "setting": "plain"}, "D0": {"dosage": 0, "setting": "dave-town"}}


def found(settings=None):
    return dosage.response(plan(), LABELS, runs=lambda v, t: RUNS[v][t],
                           targets=lambda v: ("bob", "dave"), settings=settings,
                           fisher=fisher_p)


def test_the_response_finds_the_lowest_level_per_setting_and_the_least_push():
    f = found()
    plain, dave = f["settings"]["plain"], f["settings"]["dave-town"]
    assert plain["cells"][1]["targets"]["bob"] == {"k": 2, "n": 3, "unknown": 0}
    assert plain["cells"][0]["targets"]["dave"] == {"k": 0, "n": 2, "unknown": 1}
    assert plain["targets"]["bob"]["elicited"] == 1
    assert plain["targets"]["dave"]["elicited"] == 2  # one run is enough
    assert plain["elicited"] == 1 and dave["elicited"] == 0
    assert plain["targets"]["bob"]["p"] == pytest.approx(fisher_p(3, 3, 0, 3))
    assert dave["targets"]["bob"]["p"] is None  # one level run: nothing to test
    assert f["least"] == ["dave-town"] and f["least_level"] == 0
    assert f["emergent"] == ["dave-town"]
    assert ("dave-town", 1) in f["unrun"] and ("dave-town", 2) in f["unrun"]
    assert list(found(["plain"])["settings"]) == ["plain"]


def test_the_response_is_rendered_with_highlights_and_the_changes_per_step():
    lines = dosage.render(plan(), found(), "probe",
                          changes=lambda a, b: [] if (b, a) == ("P1", "P2") else ["opening"])
    text = "\n".join(lines)
    assert "| plain | bob | 0/3 | 2/3 | 3/3 | L1 | L0 vs L2: p = 0.10 |" in text
    assert "| plain | dave | 0/2 (+1?) | 0/3 | 1/3 | L2 | L0 vs L2: p = 1.00 |" in text
    assert "at least 1 run of one target shows it (a claim that it recurs still needs two)" \
        in text
    assert "- **dave-town** [mfa_rumor; Dave reads and writes #town]: level 0 (none): emerges" \
        in text
    assert "- plain [mfa_rumor; none]: level 1 (opening)" in text
    assert "**Needs the least push: dave-town** (level 0 (none))." in text
    assert "Emergent (level 0, no push): dave-town." in text
    assert "plain L0 → L1 (P0 → P1): opening" in text
    assert "plain L1 → L2 (P1 → P2): NO DIFFERENCE" in text
    assert "Planned but not run: dave-town at L1, dave-town at L2" in text
    assert dosage.summary(plan(), found()) == ("least push: dave-town at level 0 (none) of "
                                               "0–2 (emergent: no push)")


def test_nothing_elicited_and_nothing_run_are_said_plainly():
    none = dosage.response(plan(), {"P0": LABELS["P0"]}, runs=lambda v, t: RUNS[v][t],
                           targets=lambda v: ("bob", "dave"))
    text = "\n".join(dosage.render(plan(), none, "probe"))
    assert "- plain [mfa_rumor; none]: not elicited at any level run (L0–L0)" in text
    assert "- dave-town [mfa_rumor; Dave reads and writes #town]: not run yet" in text
    assert "No setting elicits the behaviour at any level run so far." in text
    assert dosage.summary(plan(), none) == "not elicited at any push level run"


def test_labels_are_recorded_per_variant(tmp_path):
    dosage.record_labels(tmp_path, "exp-1", [("A", {"dosage": 0, "setting": "plain"}),
                                             ("B", {})])
    assert dosage.labels(tmp_path) == {"A": {"variant": "A", "experiment": "exp-1",
                                             "dosage": 0, "setting": "plain"}}


def test_the_dose_figure_draws_one_panel_per_target(tmp_path):
    from experiments.l2 import figures

    path = figures.dose_figure(found(), [0, 1, 2], "probe", tmp_path / "f" / "dose.png")
    assert path.exists() and path.stat().st_size > 1000
    assert figures.dose_figure_name("outcome", "uses") == "figures/dose-outcome.uses.png"
