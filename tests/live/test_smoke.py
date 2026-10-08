"""Live smoke test against a cheap OpenRouter model (opt-in, costs cents).

    uv run pytest --run-live -m live tests/live

Needs ``OPENROUTER_API_KEY`` in the environment or the gitignored ``.env``.
"""

from __future__ import annotations

import os

import pytest

from experiments.run import run_condition
from experiments.scenarios import SCENARIOS

MODEL = "openrouter/openai/gpt-4.1-nano"


@pytest.mark.live
def test_level_1_auditor_runs_live_on_the_release_scenario(tmp_path):
    from dotenv import load_dotenv

    load_dotenv()
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY is not set")
    from inspect_ai.model import get_model

    scenario = SCENARIOS["release_pressure"]({agent: MODEL for agent in ("pm", "coder",
                                                                          "reviewer")})
    models = {"auditor": get_model(MODEL), "judge": get_model(MODEL), "target": MODEL}
    [row] = run_condition(scenario, "l1", 1, tmp_path, models)
    assert row["status"] == "success", row["error"]
    assert row["auditor"]["wakes"] > 0
    assert row["agents"]["coder"]["posts"]
    assert row["budget"]["provider_calls"] > 0
