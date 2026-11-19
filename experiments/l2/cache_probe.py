"""Which cache setup makes OpenRouter cache L2's conversation on Claude? (issue #54)

Sends a short conversation shaped like L2's (a long system prompt, tools, then string
tool results) once per variant, and prints for each call the provider that served it and
the tokens read from and written to the cache. Caching works when each call reads about
the whole prompt of the call before it. Each variant uses its own random prefix, so no
variant reads another's (or an earlier run's) cache.

Variants:
  A    current L2 setup: Inspect's markers (5 min) + top-level cache_control (1 h)
  B5   top-level cache_control only (5 min); Inspect's markers off
  B1h  top-level cache_control only (1 h); Inspect's markers off
  C    our own breakpoints, all 1 h: last tool, system, last two messages (tool included)
  D    as C, but the message breakpoints skip tool messages (user/assistant only)

``$`` is our price table's estimate (cache writes at the 1 h rate); ``billed`` is what
OpenRouter charged, when it says. Needs OPENROUTER_API_KEY. About 4 short calls per
variant:

    uv run python -m experiments.l2.cache_probe
    uv run python -m experiments.l2.cache_probe --variants C,D --only amazon-bedrock
    uv run python -m experiments.l2.cache_probe --variants B1h,C --pause 400
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import secrets
from functools import partial
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    Model,
    get_model,
)
from inspect_ai.tool import ToolCall, ToolInfo, ToolParams

from experiments.l2 import agent, caching
from experiments.l2.cost import usage_cost

MODEL = "openrouter/anthropic/claude-opus-5.5"
VARIANTS = ("A", "B5", "B1h", "C", "D")
WORDS = ("audit", "agent", "channel", "round", "brief", "variant", "epoch", "score",
         "seat", "target", "rumour", "budget", "ledger", "schedule", "persona", "probe",
         "migration", "reviewer", "claim", "sample", "token", "cache", "provider", "log")


def _text(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def _conversation(nonce: str, calls: int) -> list[list[ChatMessage]]:
    """The messages of each call: the same prefix, one more tool result each time."""
    rng = random.Random(nonce)
    head: list[ChatMessage] = [
        ChatMessageSystem(content=f"[{nonce}] You are an auditor. " + _text(rng, 1500)),
        ChatMessageUser(content="Brief: " + _text(rng, 100)),
    ]
    out = [list(head)]
    for i in range(1, calls):
        call = ToolCall(id=f"call_{i}", function="run_experiment", arguments={"n": i})
        head = head + [
            ChatMessageAssistant(content=f"Running experiment {i}.", tool_calls=[call]),
            ChatMessageTool(content=f"Result {i}: " + _text(rng, 700),
                            tool_call_id=call.id, function="run_experiment"),
        ]
        out.append(list(head))
    return out


TOOLS = [
    ToolInfo(name="run_experiment", description="Run one experiment. " + "x " * 50,
             parameters=ToolParams()),
    ToolInfo(name="scores", description="Read scores.", parameters=ToolParams()),
]


def _model(variant: str, model: str, only: str | None) -> Model:
    extra: dict[str, Any] = {"provider": {"only": [only]}} if only else {}
    if variant == "A":
        m = agent._model(model)
        assert isinstance(m, Model)
        if only:
            m.config.extra_body = {**(m.config.extra_body or {}), **extra}
        return m
    if variant in ("B5", "B1h"):
        control = {"type": "ephemeral", **({"ttl": "1h"} if variant == "B1h" else {})}
        extra["cache_control"] = control
    config = GenerateConfig(cache_prompt=False, extra_body=extra or None)
    m = get_model(model, strict_tools=False, config=config, memoize=False)
    if variant in ("C", "D"):
        caching.install(m, partial(caching.mark, ttl="1h", tool_messages=variant == "C"))
    return m


def _provider(m: Model) -> list[tuple[str, float | None]]:
    """Record the provider and OpenRouter's billed cost of each completion ``m`` receives."""
    seen: list[tuple[str, float | None]] = []
    api = m.api
    send = api._generate_completion

    async def recorded(request: dict[str, Any], config: Any) -> Any:
        completion = await send(request, config)
        usage = getattr(completion, "usage", None)
        extra = getattr(usage, "model_extra", None) or {}
        seen.append((str(getattr(completion, "provider", None)), extra.get("cost")))
        return completion

    api._generate_completion = recorded  # type: ignore[method-assign]
    return seen


async def probe(variant: str, model: str, calls: int, only: str | None,
                pause: float = 0) -> list[dict]:
    m = _model(variant, model, only)
    providers = _provider(m)
    rows = []
    for i, messages in enumerate(_conversation(secrets.token_hex(6), calls)):
        if pause and i == calls - 1:  # does the cache outlive the default 5 minutes?
            print(f"{variant:4} pausing {pause:.0f}s before the last call", flush=True)
            await asyncio.sleep(pause)
        out = await m.generate(messages, tools=TOOLS, config=GenerateConfig(max_tokens=16))
        u = out.usage
        assert u is not None
        provider, billed = providers[-1]
        rows.append({"variant": variant, "call": i, "provider": provider, "billed": billed,
                     "input": u.input_tokens, "cache_read": u.input_tokens_cache_read or 0,
                     "cache_write": u.input_tokens_cache_write or 0,
                     "cost": round(usage_cost(model, u), 4)})
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--model", default=MODEL)
    p.add_argument("--variants", default=",".join(VARIANTS))
    p.add_argument("--calls", type=int, default=4)
    p.add_argument("--only", help="pin one OpenRouter provider, e.g. anthropic, amazon-bedrock")
    p.add_argument("--pause", type=float, default=0,
                   help="seconds to wait before the last call (e.g. 400, past 5 minutes)")
    p.add_argument("--out", type=Path, default=Path("logs/cache-probe.jsonl"))
    args = p.parse_args()
    # OPENROUTER_API_KEY from the gitignored .env: the nearest one up from here, so a
    # worktree under .worktrees/ uses the main checkout's
    load_dotenv(find_dotenv(usecwd=True))
    variants = args.variants.split(",")

    async def run_all() -> list[list[dict] | BaseException]:
        # concurrently, so a pause is waited once, not once per variant
        return await asyncio.gather(
            *(probe(v, args.model, args.calls, args.only, args.pause) for v in variants),
            return_exceptions=True)

    rows: list[dict] = []
    print(f"{'var':4} {'call':>4} {'provider':22} {'input':>7} {'read':>7} {'write':>7} "
          f"{'$':>7} {'billed':>7}")
    for v, got in zip(variants, asyncio.run(run_all()), strict=True):
        if isinstance(got, BaseException):  # report and go on to the next variant
            print(f"{v:4} failed: {type(got).__name__}: {str(got)[:200]}")
            rows.append({"variant": v, "error": f"{type(got).__name__}: {got}"})
            continue
        for r in got:
            print(f"{r['variant']:4} {r['call']:>4} {r['provider']:22} {r['input']:>7} "
                  f"{r['cache_read']:>7} {r['cache_write']:>7} {r['cost']:>7} "
                  f"{r['billed']!s:>7}")
        rows += got
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a") as f:
        for r in rows:
            f.write(json.dumps({**r, "model": args.model, "only": args.only}) + "\n")
    print(f"total ${sum(r.get('cost', 0) for r in rows):.3f}; rows appended to {args.out}")


if __name__ == "__main__":
    main()
