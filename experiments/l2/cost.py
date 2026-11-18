"""Cost per call: the provider's own figure, else a price table times tokens (spec §7).

Each model call in a sample is a model event, the agents', the scripted or LLM
auditor's and the scorers' judge and probe calls alike (checked on a mock log in
``tests/unit/test_l2_cost.py``). OpenRouter reports what a call cost in the
response it logs (``event.call.response["usage"]["cost"]``); a call with that figure
is charged it. Inspect 0.3.263's OpenRouter provider does not fill in
``ModelUsage.total_cost``, so every other call is charged from the price table.

OpenRouter routes each model to one of several providers at different prices, and
the budget is enforced from these numbers, so the table errs high: per model, the
dearest of OpenRouter's listed rate and the providers live runs were routed to (from
their reported costs in ``logs/live`` and ``logs/l2``). Inspect's ``input_tokens``
leaves out cached input (``input_tokens_cache_read`` / ``_write``), charged at the full input
rate. A usage that carries its own ``total_cost`` is charged that instead (Inspect
0.3.263's OpenRouter provider never fills it in). A model missing from the table
raises ``UnknownPrice`` rather than counting as free; offline mocks (``mockllm/``)
are free.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from inspect_ai.log import EvalLog, EvalSample
from inspect_ai.model import ModelUsage

PRICES_CHECKED = "2026-10-27"
"""When ``PRICES`` was read from openrouter.ai's model pages and the live logs."""
PRICES: dict[str, tuple[float, float]] = {
    # model: (USD per 1M input tokens, USD per 1M output tokens)
    "openrouter/openai/gpt-4.1-nano": (0.10, 0.40),  # Alice
    "openrouter/openai/gpt-4o-mini": (0.15, 0.60),  # Bob, the Advocate
    # Carol. Listed 0.075 / 0.20; live runs went mostly to Venice at these rates.
    "openrouter/mistralai/mistral-small-3.2-24b-instruct": (0.09375, 0.25),
    # Dave. Listed 0.10 / 0.32 and Novita 0.135 / 0.40, but the first L2 live run also
    # went to CoreWeave, the dearest seen, at about this rate.
    "openrouter/meta-llama/llama-3.3-70b-instruct": (0.71, 0.71),
    "openrouter/openai/gpt-4.1-mini": (0.40, 1.60),  # the judge (and L1/L0 auditor)
    "openrouter/openai/gpt-4.1": (2.00, 8.00),  # a stronger L2 (listed rate, 2026-10-28)
    "openrouter/anthropic/claude-opus-5.5": (4.00, 20.00),  # an L2 (listed rate, 2026-11-02)
    # The board study's m2 and m3 (read 2026-10-30). Listed 0.006 / 7 and 0.07 / 1.40, but
    # OpenRouter routes them across many providers: these are the dearest seen.
    "openrouter/z-ai/glm-5.2": (2.31, 8.00),
    "openrouter/z-ai/glm-5.3": (2.80, 8.80),
    # The capability ladder of "Scaling Laws for Strategic Interactions" (Zhang et al., 2026),
    # with that paper's LMArena Elo (March 31, 2026). Read from OpenRouter's endpoint lists
    # on 2026-11-05; where providers differ, the dearest listed rate.
    "openrouter/meta-llama/llama-3.1-8b-instruct": (0.22, 0.287),  # Elo 1211
    "openrouter/amazon/nova-micro-v1": (0.035, 0.14),  # Elo 1240
    "openrouter/amazon/nova-pro-v1": (0.80, 3.20),  # Elo 1290
    "openrouter/qwen/qwen-2.5-72b-instruct": (0.36, 0.40),  # Elo 1302
    "openrouter/openai/gpt-5-nano": (0.055, 0.44),  # Elo 1337 (high reasoning)
    "openrouter/deepseek/deepseek-chat": (0.32, 1.0287),  # DeepSeek V3, Elo 1358
    "openrouter/deepseek/deepseek-r1-0528": (0.571, 2.286),  # Elo 1422
    "openrouter/google/gemini-2.5-pro": (2.25, 18.00),  # Elo 1448
    "openrouter/openai/gpt-5.4": (5.00, 30.00),  # Elo 1484 (high reasoning)
    "openrouter/anthropic/claude-opus-4.6": (5.50, 27.50),  # Elo 1499 (1504 thinking)
    # The strong firm in the duopoly collusion replication. Read
    # from OpenRouter's endpoint list on 2026-11-17; most providers list 3.00 / 15.00,
    # and prompts over 200k tokens cost more.
    "openrouter/anthropic/claude-sonnet-4.5": (3.00, 15.00),
    # A cheaper L2 (listed rate on OpenRouter, 2026-11-17); a reasoning model, whose
    # reasoning tokens bill as output.
    "openrouter/openai/gpt-6.2-sol": (2.00, 10.00),
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


def reported_cost(event: Any) -> float | None:
    """What the provider said a model call cost, from the response Inspect logged."""
    call = getattr(event, "call", None)
    response = getattr(call, "response", None) or {}
    cost = (response.get("usage") or {}).get("cost") if isinstance(response, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, int | float):
        return None
    return float(cost)


def sample_cost(sample: EvalSample,
                prices: Mapping[str, tuple[float, float]] = PRICES) -> dict[str, Any]:
    """``{"usd", "by_model": {model: {input_tokens, output_tokens, usd, calls,
    reported_calls}}}`` for one sample: each call at its reported cost, else at the
    table's. A sample read without its events is priced from its usage alone."""
    events = [e for e in sample.events or [] if e.event == "model"]
    if not events:
        return usage_table(sample.model_usage or {}, prices)
    return events_cost(events, prices)


def events_cost(events: Iterable[Any],
                prices: Mapping[str, tuple[float, float]] = PRICES) -> dict[str, Any]:
    """``sample_cost`` for model events directly, e.g. a running sample's own
    (``inspect_ai.log.transcript().events``); events of other kinds are ignored."""
    by_model: dict[str, dict[str, Any]] = {}
    for event in events:
        if getattr(event, "event", None) != "model":
            continue
        usage = event.output.usage if event.output is not None else None
        reported = reported_cost(event)
        usd = reported if reported is not None else (
            usage_cost(event.model, usage, prices) if usage is not None else 0.0)
        row = by_model.setdefault(event.model, {"input_tokens": 0, "output_tokens": 0,
                                                "usd": 0.0, "calls": 0, "reported_calls": 0})
        if usage is not None:
            row["input_tokens"] += input_tokens(usage)
            row["output_tokens"] += usage.output_tokens
        row["usd"] += usd
        row["calls"] += 1
        row["reported_calls"] += reported is not None
    return {"usd": sum(m["usd"] for m in by_model.values()),
            "by_model": dict(sorted(by_model.items()))}


def log_cost(log: EvalLog, prices: Mapping[str, tuple[float, float]] = PRICES) -> dict[str, Any]:
    """A log's cost: the sum of its samples' (each sample's own entry under
    ``samples``), or, for a log read without samples, its eval-level usage."""
    if not log.samples:
        return {**usage_table(log.stats.model_usage or {}, prices), "samples": []}
    samples = [sample_cost(s, prices) for s in log.samples]
    totals: dict[str, dict[str, Any]] = {}
    for entry in samples:
        for model, row in entry["by_model"].items():
            total = totals.setdefault(model, {key: 0 for key in row})
            for key in row:
                total[key] = total.get(key, 0) + row[key]
    return {"usd": sum(s["usd"] for s in samples), "by_model": dict(sorted(totals.items())),
            "samples": samples}
