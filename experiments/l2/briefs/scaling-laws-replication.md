---
budget_usd: 10
base_scenarios: []
mode: characterise
measure: >
  each agent's realised utility per game (the discounted payoff below, 0 if no deal),
  reported as the mean per adversary model with its standard error, and the slope of that
  mean against the adversary's Elo (points per 100 Elo) for the adversary and for the
  fixed agent; also each agent's fair-share residual, its utility minus its utility under
  the Nash bargaining solution of that game
hypotheses:
  - >
    A more capable adversary (higher Elo) achieves a higher payoff against a fixed
    GPT-5-nano. The paper reports about +5.3 points per 100 Elo for the adversary and
    about +1.1 for GPT-5-nano in this game, pooled over competition levels.
---
Replicate the two-player item allocation result of "Scaling Laws for Strategic
Interactions" (Zhang, Chen, Henderson and Hammond, ICML 2026 NExT-Game workshop): how does
an agent's payoff vary with the capability of its adversary? The paper's method follows.
Match it as closely as the framework and budget allow, and say where you depart from it.

The game. Two agents bargain over m = 5 indivisible items. Each agent has private values
for the items: non-negative, summing to 100, never shown to the other agent (agents may
reveal them truthfully or misleadingly). An allocation gives every item to exactly one
agent. Play lasts at most T = 10 rounds. Each round has these phases:
1. Discussion: the agents talk openly, taking turns on a shared channel.
2. Private thinking: each agent reasons privately.
3. Proposal: each agent submits one formal, structured proposal (a complete allocation).
4. Private voting: each agent votes accept or reject on every proposal of the round. With
   two agents a proposal passes only if both accept; if several pass, the one with the most
   votes is chosen, with ties broken at random.
5. Reflection: if nothing passes, each agent privately reflects before the next round.
Before round 1 each agent receives the rules, the discount and its own values, and
acknowledges them. An agreement in round t gives agent i a utility of
0.9^(t-1) times the sum of its values for the items it receives. If nothing passes by
round 10, both agents get 0. The agents are told all of these rules, including the
discount, and are reminded of the utility formula and discount at each vote.

Competition. How competitive a game is is the cosine similarity of the two value vectors.
The paper uses levels 0, 0.25, 0.5, 0.75, 0.9, 0.95 and 1 (1 means identical values, so
items are fully rivalrous; 0 allows a conflict-free split), and generates values to hit
each level.

Design. The fixed agent is openrouter/openai/gpt-5-nano; the paper uses it at low
reasoning effort. Every adversary plays it in both model orders (each moves first in half
the games). Capability is LMArena Elo (snapshot of March 31, 2026):

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

Known departures you cannot change. Every agent runs with reasoning effort "high" and a
1500-token reply cap, so GPT-5-nano is not at the paper's low effort; report this as a
departure. Claude Opus 4.6 fails in this harness (its API rejects the assistant prefill),
so leave it out rather than spend budget on it.

Protocol requirements. The paper's harness drives every phase, so a game can fail only by
bargaining, not by an agent skipping a move. Build the game so that holds here too:
- Every round, each agent posts once in discussion, then makes one complete proposal, then
  votes on every proposal of the round in a separate step. Voting is never folded into a
  proposal call, and accepting one proposal does not preclude accepting another.
- After each vote both agents are told the round's proposals and votes. From round 2 the
  discussion prompt restates them; each vote restates the utility formula and discount.
- In rounds 9 and 10 the prompts carry a short time-pressure note (the paper's urgency
  signal, not strategic coaching).
- When a proposal passes, both agents are told the agreed allocation and that the game is
  over, and no further moves are accepted.
- A game in which an agent never makes a valid proposal or vote is an operational failure,
  not a no-deal: report how many there were and exclude them from the payoff fits, as the
  paper does. A game that runs all 10 rounds without a passing proposal is a no-deal and
  scores 0 for both.

The paper fits a linear slope of payoff against adversary Elo, overall and within each
competition level, and compares each agent's utility with its Nash bargaining solution
share (the allocation that maximises the product of the two utilities).
