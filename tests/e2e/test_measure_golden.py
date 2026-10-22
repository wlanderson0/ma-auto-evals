"""The measured rows must not change while measurements move into scorers."""

from __future__ import annotations

import json

import pytest

from tests.support.golden import CASES, case_rows, path_for


@pytest.mark.parametrize("name, condition", CASES)
def test_measured_rows_match_the_recorded_golden_rows(tmp_path, name, condition):
    assert case_rows(tmp_path, name, condition) == json.loads(path_for(name, condition).read_text())
