"""The board study's analysis script (plan ``docs/level2-milestone6.md``, scope item 5):
its statistics on known answers, and the report on a mock run of the grid. Offline."""

from __future__ import annotations

from pathlib import Path

import pytest

from experiments.l2.analysis.board_study import (
    chi2_sf,
    holm,
    interaction,
    report,
    smallest_detectable,
)
from experiments.l2.experiment import run_experiment
from tests.support.l2_audit import InProcess

BRIEFS = Path(__file__).resolve().parents[2] / "experiments" / "l2" / "briefs"


def test_holm_adjusts_in_order_and_stays_monotone():
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm([0.5, 0.9]) == pytest.approx([1.0, 1.0])


@pytest.mark.parametrize("n, need", [(10, 7), (20, 7), (30, 8)])
def test_smallest_detectable_matches_the_plans_figures(n, need):
    """The plan: with 1 use without the post, the post arm needs 7/10, 7/20, 8/30."""
    assert smallest_detectable(1, n, n) == need


def test_smallest_detectable_is_none_when_nothing_would_do():
    assert smallest_detectable(19, 20, 20) is None


def test_chi2_tail_at_the_usual_critical_values():
    assert chi2_sf(3.841, 1) == pytest.approx(0.05, abs=1e-3)
    assert chi2_sf(5.991, 2) == pytest.approx(0.05, abs=1e-3)


def test_interaction_tells_equal_from_opposite_effects():
    same = {("a", 1): (15, 20), ("a", 0): (5, 20), ("b", 1): (15, 20), ("b", 0): (5, 20)}
    stat, p = interaction(same)
    assert stat == pytest.approx(0, abs=1e-6) and p == pytest.approx(1, abs=1e-6)
    opposite = {("a", 1): (15, 20), ("a", 0): (5, 20), ("b", 1): (5, 20), ("b", 0): (15, 20)}
    stat, p = interaction(opposite)
    assert stat > 10 and p < 0.01
    three = {**opposite, ("c", 1): (10, 20), ("c", 0): (10, 20)}  # df 2
    assert interaction(three)[1] < 0.01


def test_interaction_has_no_finite_fit_with_an_empty_or_full_cell():
    assert interaction({("a", 1): (0, 20), ("a", 0): (5, 20),
                        ("b", 1): (5, 20), ("b", 0): (5, 20)}) is None
    assert interaction({("a", 1): (20, 20), ("a", 0): (5, 20),
                        ("b", 1): (5, 20), ("b", 0): (5, 20)}) is None


def test_the_report_covers_every_cell_on_a_mock_grid(tmp_path):
    out, calibration = tmp_path / "grid", tmp_path / "calibration"
    run_experiment(BRIEFS / "board-study-grid.md", 1, out, mock=True, launch=InProcess())
    run_experiment(BRIEFS / "bench-board-calibration.md", 1, calibration, mock=True,
                   launch=InProcess())
    text = report(out, calibration=[calibration, calibration])  # pooled: 2 of each
    for model in ("gpt-4.1-mini", "GLM 5.2", "GLM 5.3"):
        for rule in ("plain", "soft"):
            assert f"| {model} | {rule} |" in text
    assert text.count("| Holm p |") == 1
    primary = text.split("### Primary")[1].split("###")[0]
    assert sum(line.startswith("| ") for line in primary.splitlines()) == 1 + 9  # header + 9
    assert "post x model, soft rule" in text and "post x rule, GLM 5.3" in text
    assert "off-dose" in text and "Milestone 5 check" in text
    check = text.split("Milestone 5 check")[1]
    assert check.count("| 0/1 | 0/2 |") == 6  # 3 targets x 2 arms, 1 epoch each


def test_an_experiment_without_logs_is_an_error(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="no samples"):
        report(tmp_path / "empty")
