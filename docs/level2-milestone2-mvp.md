# Level 2 milestone 2: LLM L2 MVP

Status: draft. Builds on milestone 1 (fixed grid, branch `sarah/level2-grid`) and implements a first cut of `docs/level2-interface.md` §10 steps 3–4.

## Goal

A real LLM L2 auditor that runs the research loop end to end (hypothesis → variants → run → read results → decide or conclude) under a hard money budget, and leaves a full record. For now we are testing that the pipeline works, not trying to get useful findings.

## Scope

**In**
- An LLM agent with tools, given a brief.
- Variants built only from ready-made mutations (§3.2) on existing scenarios, starting from controls the brief provides.
- Inner conditions `baseline` and `scripted` only.
- An audit-wide budget ledger with a hard stop.
- An audit record and a final report.

**Out (later milestones)**
- Scenarios written by L2 (§3.5) and the generic mocks they need.
- The code workspace (§6): L2 reads results only through the query tools.
- Inner L0/L1 auditors and the combined policy (§10 step 5).
- Cosmos sync, tags, the realism check, a log-reading `exposure`, and native mid-run changes in our Orbit fork.
- Stopping a sample mid-run when the budget runs out (L2-11's in-progress case).

## Decisions

- **L2 model:** `openrouter/openai/gpt-4.1-mini`, through the same OpenRouter path as the agents. It is already in the price table (it is the judge and the L1/L0 auditor). It can be upgraded once the pipeline works.
- **Agent framework:** Inspect's `react` agent with tools, like L1, so its messages, tool calls and token use land in an Inspect log the ledger can read. L2 runs as its own Inspect task; `run_experiment` launches each epoch in a subprocess, as milestone 1 already does, so no eval is started inside the running one.
- **Starting point (controls):** a variant L2 designs is a copy of a control from the brief with one or more mutations added (§3.1: a variant is defined against its base; here the control's mutations and `inner` are copied, then changed). The brief gives the controls in a new optional field, `controls`, with the same format as `variants`; they carry the scripted opening and the seat's persona line, so L2 never writes setup or opening text itself. A variant that does not start from a control is rejected by `run_experiment`. `brief.py` rejects unknown fields today, so `controls` is added to its parser (as optional) and to the parameter table in `level2-interface.md` §2.
- **Smoke-test brief:** `briefs/milestone2.md`, made from `briefs/milestone1.md` with:
  - `base_scenarios: [mfa_rumor]` only. In the milestone 1 live run Alice was off-dose in all 7 Kestrel runs (her model, `gpt-4.1-nano`, did not follow the dose lines), so Kestrel results would mislead L2. Kestrel comes back once Alice's dose is fixed.
  - `variants` removed, and `controls: [M0, M6]` (M6 so that L2 can test the Advocate against the right control).
  - the same `measure`, so L2 cannot change it.
- **Hard stop:** once spent plus the reserve reaches the budget, no new experiment or epoch starts, and every tool except `budget`, `request_review` and `end_audit` refuses with the reason. A launched epoch runs to completion, so spending can exceed the budget by up to one epoch, as in milestone 1; the record says so. L2's own spend is read from its model events and checked at every tool call; a token limit on the agent is the backstop. **Risk, check first:** it is not yet confirmed that a tool can read its own running sample's token usage in Inspect; if it can't, the token limit becomes the main guard on L2's own spend.
- **Reserve:** 10% of the budget (or more, once L2's calls get dear; §7), held back from experiments for L2's final calls (§7).

## Audit layout

One directory per L2 audit:

```
<audit>/
  brief.md                 the brief as given
  decisions.jsonl          L2's choices on what the brief left open, with reasons
  trace.jsonl              every tool call and its (shortened) result, in order
  ledger.json              spend by kind (experiments, L2's own calls), reserve, remaining
  experiments/exp-<n>/     one milestone 1 audit directory per run_experiment call
    brief.md               a small generated brief holding that call's variants
    variants.jsonl, runs.jsonl, decisions.json, cost.json, logs/...
  l2/                      L2's own Inspect log
  report.md                the final conclusions
```

`run_experiment` writes the per-call brief and calls the milestone 1 `run_experiment` on `experiments/exp-<n>/`, so the milestone 1 record format is unchanged.

## Steps

1. **Audit budget ledger (§7).**
   - Total spend = the cost of every experiment (milestone 1 `cost.json`) plus L2's own calls.
   - Provides `budget()` (spent, remaining, reserve, by kind), the hard stop above, and a warning when an experiment's estimate exceeds what is left, which needs `confirm=True` to launch anyway.
   - Tests: L2-10, L2-12, L2-13, L2-14, and L2-11 for "no new experiment or epoch starts" (not the in-progress case).
2. **Tools.** All outputs are short by default (tables, slices, the first N results), with an argument to ask for more.
   - `run_experiment(variants, epochs, confirm=False)`: variants as JSON, each naming the control it starts from; runs the milestone 1 validity checks first and returns rejections without running anything. Returns per-variant status, warnings, cost and the remaining budget.
   - `list_runs()`: every variant run so far in this audit, with its experiment, status and epochs.
   - `scores(variant_ids, scorer)` and `compare(a, b, scorer, rounds=None)`: the milestone 1 `Results` functions, across all experiments in the audit.
   - `transcript(variant_id, epoch, agent=None, max_posts=20)`: the posts of one sample in order, each with its turn, channel and authorship label (`agent:<a>` or `auditor-as:<a>`); optionally one agent's only.
   - `find(pattern, variant_ids=None, agent=None, max_results=20)`: every post matching a regex across the audit's runs, each with its variant, epoch, turn and authorship label.
   - `note_decision(field, value, reason)`: records L2's choice on something the brief left open (e.g. `mode`), in `decisions.jsonl`.
   - `budget()`.
   - `end_audit(conclusions)`: ends the audit and writes `report.md` (step 3).
   - Test: L2-18 (`find` and `transcript` label authorship).
3. **Audit record (§8):** the layout above. `report.md` holds the conclusions from `end_audit`, the tables they rest on, the search counts (variants, scorers, comparisons tried), where L2 departed from the guidance, and the ledger.
4. **Fake L2.** A scripted policy that issues fixed tool calls (note a decision, run a two-variant experiment, read scores, compare, end), so steps 1–3 are tested end to end on the mock models, with no LLM cost.
5. **LLM L2 agent.**
   - The system prompt carries the §6.3 reading guidance (one change per comparison, results per target, equal epochs, leads vs findings) and says how variants are built from the controls.
   - The agent uses the tools from step 2.
   - It finishes with `end_audit(conclusions)`. The conclusions must include the search counts and say where L2 departed from the guidance.
6. **Smoke tests (small budget, e.g. $0.50; the milestone 1 live run cost about $0.002 per variant-epoch, so the one-epoch overrun is negligible):**
   - (a) **Analysis only:** the query tools point at the milestone 1 live audit directory, `run_experiment` is off, and the question is "From these runs, what can and can't be concluded about how topology affects whether Dave adopts the claim?"
   - (b) **Full loop:** `briefs/milestone2.md`.
   - Pass means it runs to completion within budget (plus at most one epoch) and produces a complete record. Result quality is not judged yet; in (a) the milestone 1 live run has only 1 epoch per variant, so L2 saying little can be concluded is the expected, correct answer.

## Implementation notes

- **Branch:** work on `sarah/level2-grid`; no new branch.
- **Where code goes:** new modules in `experiments/l2/` next to the milestone 1 ones, e.g. `audit.py` (audit directory, ledger, hard stop), `tools.py` (step 2 tools), `fake.py` (step 4) and `agent.py` (step 5). Reuse milestone 1 code (`experiment.py`, `results.py`, `cost.py`, `validity.py`, `brief.py`) rather than copying it. Tests go in `tests/unit`, `tests/contract` and `tests/e2e` as for milestone 1.
- **Order and checkpoints:**
  1. Spike first: confirm whether a tool can read its own running sample's token usage in Inspect (Decisions, hard stop). Report the result before building the ledger on it.
  2. Steps 1–4, test-first, on the mock models only: no live provider calls.
  3. **Stop for review** before step 5 and before any OpenRouter spend.
  4. Step 5, then the smoke tests in step 6, each run only after approval.
- **How it is run:** a CLI entry point like milestone 1's, e.g. `uv run python -m experiments.l2.audit --brief experiments/l2/briefs/milestone2.md [--mock] [--analysis-only <audit dir>]`. `uv run pytest` must pass after every step, with milestone 1 tests unchanged.
- **Tool outputs:** follow the style of milestone 1's `Results` functions (small plain tables, per target, k of n), short by default.

## Next iteration

- Compare L2 with the fixed grid at the same budget, and a minimal brief against a detailed one (§9.4).
- Fix Alice's dose (e.g. a stronger model for the seat) and bring Kestrel back.
- Try new briefs and variants.
- Add the code workspace.
