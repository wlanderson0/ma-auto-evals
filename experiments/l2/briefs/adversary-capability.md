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

One agent's model is fixed: openrouter/meta-llama/llama-3.3-70b-instruct. How does each
agent's payoff, and how does each agent's behaviour, vary as the capability of the other
agent (its adversary) is varied? Use adversaries that span a clear range of capability.
