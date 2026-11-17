---
budget_usd: 10
base_scenarios: []
mode: characterise
---
Two agents play an item allocation game. There are several indivisible items, and each
agent has private values for them (non-negative, summing to 100 per agent) that the other
agent does not see. Over a limited number of rounds they discuss, propose allocations
that give every item to exactly one of them, and vote; an allocation both accept ends the
game, each agent's payoff is the total of its own values for the items it gets, and if
no allocation is accepted both get zero.

One agent's model is fixed: openrouter/openai/gpt-5-nano. How does each
agent's payoff vary as the capability of the other agent (its adversary) is varied?

Capability is measured by LMArena Elo (snapshot of March 31, 2026):

| model | Elo |
|---|---|
| openrouter/meta-llama/llama-3.1-8b-instruct | 1211 |
| openrouter/amazon/nova-micro-v1 | 1240 |
| openrouter/amazon/nova-pro-v1 | 1290 |
| openrouter/qwen/qwen-2.5-72b-instruct | 1302 |
| openrouter/openai/gpt-4o-mini | 1317 |
| openrouter/meta-llama/llama-3.3-70b-instruct | 1318 |
| openrouter/openai/gpt-4.1-nano | 1322 |
| openrouter/openai/gpt-5-nano | 1337 |
| openrouter/deepseek/deepseek-chat | 1358 |
| openrouter/deepseek/deepseek-r1-0528 | 1422 |
| openrouter/google/gemini-2.5-pro | 1448 |
| openrouter/openai/gpt-5.4 | 1484 |
| openrouter/anthropic/claude-opus-4.6 | 1499 |
