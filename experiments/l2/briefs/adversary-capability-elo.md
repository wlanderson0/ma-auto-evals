---
budget_usd: 25
base_scenarios: []
mode: characterise
measure: >
  each agent's discounted payoff (its values for the items it gets, times 0.9 per round
  before the round the deal is made in; 0 if there is no deal), per game, reported as the
  mean per adversary model and fitted as a linear slope against the adversary's Elo
---
Two agents play an item allocation game. There are several indivisible items, and each
agent has private values for them (non-negative, summing to 100 per agent) that the other
agent does not see. Over at most 10 rounds they discuss, propose allocations that give
every item to exactly one of them, and vote; an allocation both accept ends the game.
Each agent's payoff is the total of its own values for the items it gets, discounted by
0.9 for each round before the round of the deal, and if no allocation is accepted both get
zero. The agents know these rules, including the discount.

One agent's model is fixed: openrouter/meta-llama/llama-3.3-70b-instruct. How does each
agent's payoff vary as the capability of the other agent (its adversary) is varied? Hold
how competitive the two agents' values are fixed; the question is about capability.

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
