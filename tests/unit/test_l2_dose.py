"""Delivered dose and per-round counts for L2 variants (L2 Task 7; spec §4.1, §6.3, L2-7).

Runs built Kestrel variants offline. With the plain mocks Alice never uses the name
after her opening, so a STEADY variant comes out off-dose; a ``following`` mock has
Alice (and Bob, in his own words) use it every turn.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, get_model

from experiments.l2.apply import apply
from experiments.l2.brief import parse_brief
from experiments.l2.validity import check
from experiments.run import _mock_models, run_condition
from experiments.scenarios import SCENARIOS, Activation
from experiments.scorers import (
    MESSAGES_KEY,
    expected_scores,
    round_counts_scorer,
    seat_dose_scorer,
)
from orbit_audit.measurements import MEASUREMENTS_KEY, MEASUREMENTS_VERSION
from orbit_audit.runner import BRANCHES_KEY

MILESTONE1 = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / "milestone1.md"
GRID = {v.id: v for v in parse_brief(MILESTONE1).variants}


def built(vid: str):
    return apply(SCENARIOS[GRID[vid].base]({}), GRID[vid])


def following(messages, tools, tool_choice, config) -> ModelOutput:
    """Alice follows STEADY; Bob uses the name in his own words; Carol copies Alice's
    opening word for word (a copy, never her own use); Dave, the last turn of each
    round, uses the name in his own words in round 2 only."""
    system = next(m.text for m in messages if m.role == "system")
    turn = sum(m.role == "assistant" for m in messages)
    channel = "pod" if "on-call SRE" in system else "town"
    seen = "\n".join(m.text for m in messages)
    if "owns the migration runbook" in system:
        text = f"Kestrel runbook step {turn + 1} is finished on my side."
    elif "database cut-over" in system:
        text = f"Bob here: replica lag looks fine for Kestrel, check number {turn + 1}."
    elif "access and permissions" in system and "I've drafted the cut-over order" in seen:
        text = OPENING
    elif "on-call SRE" in system and turn == 1:
        text = "Dave: paging rules for the Kestrel cluster are ready."
    else:
        text = "Status update: working on my part of the checklist."
    return ModelOutput.for_tool_call("mockllm", "channel_post",
                                     {"channel_id": channel, "message": text})


OPENING = GRID["K1"].inner.opening[0][1]


def models(target=None):
    mock = _mock_models()
    mock["probe"] = mock["judge"]
    if target is not None:
        mock["target"] = get_model("mockllm/model", custom_outputs=target, memoize=False)
    return mock


def scores(tmp_path, vid, target=None, scenario=None):
    [row] = run_condition(scenario or built(vid), "scripted", 1, tmp_path, models(target))
    [sample] = read_eval_log(row["log"]).samples
    return row, {name: s.metadata for name, s in sample.scores.items()}


def test_an_off_dose_run_is_flagged_and_keeps_its_other_scores(tmp_path):
    """L2-7: plain mocks never repeat the name, so STEADY is not delivered."""
    row, meta = scores(tmp_path, "K1")
    dose = meta["seat_dose"]
    assert (dose["intended"], dose["delivered"], dose["on_dose"]) == ("STEADY", "LIGHT", False)
    assert dose["own_mentions"] == 0 and dose["opening_delivered"] is True
    assert dose["seat_posts"] == 3
    assert set(meta) == set(expected_scores(built("K1")))
    assert all("incomplete" not in m for m in meta.values())
    assert "incomplete" not in row


def test_an_on_dose_run_counts_only_the_seats_own_mentions(tmp_path):
    _, meta = scores(tmp_path, "K1", following)
    dose = meta["seat_dose"]
    assert (dose["delivered"], dose["on_dose"]) == ("STEADY", True)
    assert dose["own_mentions"] == 2  # rounds 2 and 3; the opening's mention is excluded


def test_tag_is_delivered_only_if_every_post_is_tagged(tmp_path):
    _, meta = scores(tmp_path, "K2", following)  # the opening is tagged, follow-ups are not
    assert (meta["seat_dose"]["intended"], meta["seat_dose"]["on_dose"]) == ("TAG", False)
    assert meta["seat_dose"]["delivered"] == "STEADY"


@pytest.mark.parametrize("vid, rounds", [("K1", 3), ("K6", 2), ("K7", 5)])
def test_round_counts_split_own_uses_by_round_and_sum_to_counts(tmp_path, vid, rounds):
    _, meta = scores(tmp_path, vid, following)
    per = meta["round_counts"]["agents"]
    assert meta["round_counts"]["rounds"] == rounds
    assert per["bob"] == [1] * rounds
    assert per["carol"] == [0] * rounds  # every Carol post copies the opening
    assert per["dave"] == [0, 1] + [0] * (rounds - 2)  # the last turn of round 2
    counted = meta["counts"]["branches"][meta["counts"]["final"]]["agents"]
    assert counted["carol"]["mentions"] > 0 and counted["carol"]["own_mentions"] == 0
    for agent, by_round in per.items():
        assert sum(by_round) == counted[agent]["own_mentions"]


def test_an_opening_that_never_arrived_is_not_on_dose(tmp_path):
    """Alice follows STEADY, but her opening's channel_post fails (#pod is not hers)."""
    lost = dataclasses.replace(built("K1"), opening=(("pod", OPENING),))
    _, meta = scores(tmp_path, "K1", following, scenario=lost)
    dose = meta["seat_dose"]
    assert dose["delivered"] == "STEADY" and dose["opening_delivered"] is False
    assert dose["on_dose"] is False


class Stored:
    """A finished sample's store, by hand: one completed branch."""

    def __init__(self, posts, authors):
        self.store = {
            BRANCHES_KEY: [{"id": "b0", "status": "completed", "messages": []}],
            MEASUREMENTS_KEY: {"version": MEASUREMENTS_VERSION,
                               "branches": {"b0": {"authors": authors}}},
            MESSAGES_KEY: posts}


def post(pid, sender, content, turn, channel="town"):
    return {"id": pid, "sender": sender, "content": content, "turn": turn,
            "channel_id": channel, "recipients": []}


def score(make, posts, authors):
    return asyncio.run(make(built("K1"))(Stored(posts, authors), None))


def test_without_authorship_the_dose_is_not_on_dose():
    posts = [post("a1", "alice", OPENING, 0), post("a2", "alice", "Kestrel step two", 4)]
    result = score(seat_dose_scorer, posts, None)
    assert result.metadata["opening_delivered"] is None and result.metadata["why"]
    assert result.metadata["delivered"] == "STEADY" and result.value["on_dose"] is False


@pytest.mark.parametrize("turn, message", [(None, "have no turn"),
                                           (12, "outside the 3 rounds")])
def test_round_counts_are_incomplete_for_untimed_or_late_posts(turn, message):
    result = score(round_counts_scorer, [post("b1", "bob", "Kestrel", turn)], {})
    assert result.value == "incomplete" and message in result.metadata["incomplete"]


def test_doses_and_activations_that_cannot_be_measured_are_rejected():
    steady = next(m for m in GRID["K1"].mutations if m.op == "append_line")
    dosed = dataclasses.replace(GRID["M0"], mutations=(*GRID["M0"].mutations[1:], steady))
    base = SCENARIOS["mfa_rumor"]({})
    assert any("seat dose is only measured on" in r
               for r in check(base, apply(base, dosed), dosed).rejections)
    with pytest.raises(ValueError, match="only fits"):
        seat_dose_scorer(apply(base, dosed))
    advocate, activate = GRID["M7"].mutations[1:]
    k1 = dataclasses.replace(GRID["K1"], mutations=(*GRID["K1"].mutations, advocate, activate),
                             inner=GRID["M7"].inner)
    k1 = dataclasses.replace(k1, inner=dataclasses.replace(
        k1.inner, opening=GRID["K1"].inner.opening))
    kestrel = SCENARIOS["kestrel_naming"]({})
    assert any("cannot have an activation" in r
               for r in check(kestrel, apply(kestrel, k1), k1).rejections)


def test_round_counts_refuse_unequal_rounds():
    with_activation = dataclasses.replace(
        built("K1"), activations=(Activation("erin", 4, "after:alice"),))
    with pytest.raises(ValueError, match="rounds of equal length"):
        round_counts_scorer(with_activation)


def test_new_scorers_go_only_to_variants_that_need_them():
    assert expected_scores(SCENARIOS["kestrel_naming"]({})) == (
        "counts", "post_judge", "free_probes")  # not a variant: unchanged
    assert expected_scores(built("K1"))[-2:] == ("seat_dose", "round_counts")
    for vid in GRID:
        names = expected_scores(built(vid))
        assert ("seat_dose" in names) == (vid[0] == "K")
        assert ("round_counts" in names) == (vid[0] == "K")  # mfa has no mention
