"""Cost from a price table times tokens (spec ``docs/level2-interface.md`` §7).

Inspect 0.3.263's OpenRouter provider does not fill in ``ModelUsage.total_cost``,
so cost is computed from each sample's per-model token usage. A sample's
``model_usage`` covers every call made in it: the agents, the scripted or LLM
auditor, and the scorers' judge and probe calls (checked on a mock log in
``tests/unit/test_l2_cost.py``).

The budget is enforced from these numbers, so they err high. OpenRouter routes
each model to one of several providers at different prices: the table holds, per
model, the dearer of OpenRouter's listed rate and the rate of the provider live
runs were actually routed to (read from the providers' reported costs in
``logs/live``). Inspect's ``input_tokens`` leaves out cached input
(``input_tokens_cache_read`` / ``_write``), which is charged here at the full input
rate. A usage that carries its own ``total_cost`` is charged that instead (Inspect
0.3.263's OpenRouter provider never fills it in). A model missing from the table
raises ``UnknownPrice`` rather than counting as free; offline mocks (``mockllm/``)
are free.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from inspect_ai.log import EvalLog, EvalSample
from inspect_ai.model import ModelUsage

PRICES_CHECKED = "2026-10-26"
"""When ``PRICES`` was read from openrouter.ai's model pages and the live logs."""
PRICES: dict[str, tuple[float, float]] = {
    # model: (USD per 1M input tokens, USD per 1M output tokens)
    "openrouter/openai/gpt-4.1-nano": (0.10, 0.40),  # Alice
    "openrouter/openai/gpt-4o-mini": (0.15, 0.60),  # Bob, the Advocate
    # Carol. Listed 0.075 / 0.20; live runs went mostly to Venice at these rates.
    "openrouter/mistralai/mistral-small-3.2-24b-instruct": (0.09375, 0.25),
    # Dave. Listed 0.10 / 0.32; live runs went to Novita at these rates.
    "openrouter/meta-llama/llama-3.3-70b-instruct": (0.135, 0.40),
    "openrouter/openai/gpt-4.1-mini": (0.40, 1.60),  # the judge (and L1/L0 auditor)
}
FREE_PREFIXES = ("mockllm/",)
"""Offline mock models: no provider, no cost."""


class UnknownPrice(KeyError):
    """A model used in a log has no entry in ``PRICES``."""


def price(model: str, prices: Mapping[str, tuple[float, float]] = PRICES) -> tuple[float, float]:
    if not isinstance(model, str):
        raise UnknownPrice(f"no price for model {model!r} (not a model name)")
    if model in prices:
        return prices[model]
    if model.startswith(FREE_PREFIXES):
        return 0.0, 0.0
    raise UnknownPrice(f"no price for {model!r}: add it to experiments/l2/cost.py PRICES "
                       f"(checked {PRICES_CHECKED})")


def usage_cost(model: str, usage: ModelUsage,
               prices: Mapping[str, tuple[float, float]] = PRICES) -> float:
    if usage.total_cost is not None:
        return usage.total_cost
    per_input, per_output = price(model, prices)
    return (input_tokens(usage) * per_input + usage.output_tokens * per_output) / 1_000_000


def input_tokens(usage: ModelUsage) -> int:
    """All input tokens, cached ones included (Inspect counts those separately)."""
    return (usage.input_tokens + (usage.input_tokens_cache_read or 0)
            + (usage.input_tokens_cache_write or 0))


def usage_table(usage: Mapping[str, ModelUsage],
                prices: Mapping[str, tuple[float, float]] = PRICES) -> dict[str, Any]:
    by_model = {
        model: {"input_tokens": input_tokens(u), "output_tokens": u.output_tokens,
                "usd": usage_cost(model, u, prices)}
        for model, u in sorted(usage.items())}
    return {"usd": sum(m["usd"] for m in by_model.values()), "by_model": by_model}


def sample_cost(sample: EvalSample,
                prices: Mapping[str, tuple[float, float]] = PRICES) -> dict[str, Any]:
    """``{"usd", "by_model": {model: {input_tokens, output_tokens, usd}}}`` for one sample."""
    return usage_table(sample.model_usage or {}, prices)


def log_cost(log: EvalLog, prices: Mapping[str, tuple[float, float]] = PRICES) -> dict[str, Any]:
    """A log's cost: the sum of its samples' (each sample's own entry under
    ``samples``), or, for a log read without samples, its eval-level usage."""
    if not log.samples:
        return {**usage_table(log.stats.model_usage or {}, prices), "samples": []}
    samples = [sample_cost(s, prices) for s in log.samples]
    totals: dict[str, dict[str, Any]] = {}
    for entry in samples:
        for model, row in entry["by_model"].items():
            total = totals.setdefault(model, {"input_tokens": 0, "output_tokens": 0, "usd": 0.0})
            for key in total:
                total[key] += row[key]
    return {"usd": sum(s["usd"] for s in samples), "by_model": dict(sorted(totals.items())),
            "samples": samples}
