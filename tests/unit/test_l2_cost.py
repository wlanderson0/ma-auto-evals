"""Per-sample cost from a price table (L2 Task 8; spec §7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, ModelUsage, get_model

from experiments.l2.apply import apply
from experiments.l2.brief import parse_brief
from experiments.l2.cost import (
    PRICES,
    UnknownPrice,
    events_cost,
    log_cost,
    price,
    sample_cost,
    usage_cost,
)
from experiments.run import AUDITOR, JUDGE, TARGETS, _mock_models, run_condition
from experiments.scenarios import SCENARIOS

MILESTONE1 = Path(__file__).parents[2] / "experiments" / "l2" / "briefs" / "milestone1.md"
GRID = {v.id: v for v in parse_brief(MILESTONE1).variants}
TEST_PRICES = {"mockllm/model": (2.0, 10.0), "mockllm/judge": (1.0, 4.0)}


def target(messages, tools, tool_choice, config) -> ModelOutput:
    system = next(m.text for m in messages if m.role == "system")
    channel = "pod" if "on-call SRE" in system else "town"
    output = ModelOutput.for_tool_call("mockllm", "channel_post",
                                       {"channel_id": channel, "message": "On it."})
    output.usage = ModelUsage(input_tokens=100, output_tokens=10, total_tokens=110)
    return output


def judge(messages, tools, tool_choice, config) -> ModelOutput:
    output = ModelOutput.from_content("mockllm", "NO")
    output.usage = ModelUsage(input_tokens=7, output_tokens=1, total_tokens=8)
    return output


@pytest.fixture(scope="module")
def stored_log(tmp_path_factory, offline_module):
    """M0 on mocks that report tokens, written to disk and read back."""
    models = _mock_models()
    models["target"] = get_model("mockllm/model", custom_outputs=target, memoize=False)
    models["judge"] = models["probe"] = get_model("mockllm/judge", custom_outputs=judge,
                                                  memoize=False)
    scenario = apply(SCENARIOS["mfa_rumor"]({}), GRID["M0"])
    [row] = run_condition(scenario, "scripted", 1, tmp_path_factory.mktemp("cost"), models)
    return read_eval_log(row["log"])


def test_sample_cost_counts_agents_and_scorers(stored_log):
    [sample] = stored_log.samples
    cost = sample_cost(sample, TEST_PRICES)
    model, scorer = cost["by_model"]["mockllm/model"], cost["by_model"]["mockllm/judge"]
    # 12 turns, the first scripted (no model call): 11 agent generations.
    assert (model["input_tokens"], model["output_tokens"]) == (1100, 110)
    assert model["usd"] == pytest.approx((1100 * 2.0 + 110 * 10.0) / 1e6)
    # The judge (one call per target post: 9) and the probe (3 targets) are in the sample.
    assert (scorer["input_tokens"], scorer["output_tokens"]) == (84, 12)
    assert cost["usd"] == pytest.approx(model["usd"] + scorer["usd"])


def test_log_cost_sums_its_samples(stored_log):
    cost = log_cost(stored_log, TEST_PRICES)
    assert cost["usd"] == pytest.approx(sample_cost(stored_log.samples[0], TEST_PRICES)["usd"])
    assert [s["usd"] for s in cost["samples"]] == [cost["usd"]]
    header = read_eval_log(stored_log.location, header_only=True)
    assert log_cost(header, TEST_PRICES)["usd"] == pytest.approx(cost["usd"])


def test_mocks_are_free_and_unknown_models_raise(stored_log):
    assert log_cost(stored_log)["usd"] == 0.0
    with pytest.raises(UnknownPrice, match="openrouter/acme/unknown"):
        usage_cost("openrouter/acme/unknown",
                   ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2))


def test_cached_input_is_charged_at_the_input_rate():
    """Inspect's input_tokens excludes cached tokens; leaving them out under-counts."""
    usage = ModelUsage(input_tokens=100, output_tokens=10, input_tokens_cache_read=900,
                       input_tokens_cache_write=50, total_tokens=1060)
    assert usage_cost("mockllm/model", usage, TEST_PRICES) == pytest.approx(
        (1050 * 2.0 + 10 * 10.0) / 1e6)


def test_a_reported_total_cost_wins_and_a_missing_model_has_no_price():
    usage = ModelUsage(input_tokens=100, output_tokens=10, total_tokens=110, total_cost=0.5)
    assert usage_cost("openrouter/openai/gpt-4o-mini", usage) == 0.5
    with pytest.raises(UnknownPrice, match="not a model name"):
        price(None)


def test_every_model_a_live_grid_run_uses_has_a_price():
    agents = {model for targets in TARGETS.values() for model in targets.values()}
    advocate = {m.args["model"] for v in GRID.values() for m in v.mutations
                if m.op == "add_agent"}
    for model in agents | advocate | {JUDGE, AUDITOR}:
        assert price(model) == PRICES[model]


def test_a_calls_reported_cost_is_used_and_the_table_covers_the_rest():
    from types import SimpleNamespace

    def event(model, cost=None, tokens=(1000, 100)):
        usage = ModelUsage(input_tokens=tokens[0], output_tokens=tokens[1],
                           total_tokens=sum(tokens))
        call = None if cost is None else SimpleNamespace(response={"usage": {"cost": cost}})
        return SimpleNamespace(event="model", model=model, output=SimpleNamespace(usage=usage),
                               call=call)

    llama = "openrouter/meta-llama/llama-3.3-70b-instruct"
    sample = SimpleNamespace(events=[event(llama, 0.002), event(llama), SimpleNamespace(
        event="info")], model_usage={})
    cost = sample_cost(sample)
    table = usage_cost(llama, ModelUsage(input_tokens=1000, output_tokens=100,
                                         total_tokens=1100))
    row = cost["by_model"][llama]
    assert (row["calls"], row["reported_calls"]) == (2, 1)
    assert row["usd"] == pytest.approx(0.002 + table)
    assert (row["input_tokens"], row["output_tokens"]) == (2000, 200)
    assert cost["usd"] == pytest.approx(row["usd"])


def test_events_cost_prices_a_running_samples_events_like_its_log(stored_log):
    """What L2's ledger uses on its own transcript: model events only, same figures."""
    [sample] = stored_log.samples
    assert len({e.event for e in sample.events}) > 1  # other kinds are skipped
    assert events_cost(sample.events, TEST_PRICES) == sample_cost(sample, TEST_PRICES)
    assert events_cost([], TEST_PRICES) == {"usd": 0, "by_model": {}}
