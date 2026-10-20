# Inspect-native measurements (issue #19, stage 1)

Date: 2026-10-20 · Branch: `feat/inspect-native-measurements` · Inspect: 0.3.263

## Problem

`measure()` in `experiments/run.py` runs after `inspect_eval` returns. It asks
each target agent a private belief probe, has a judge classify each target's
posts, extracts the reviewer's verdict, asks free-text probes, and computes
mention, exposure and copy counts, tool-recorded outcomes and per-branch plan
checks. The results go only to `results.json`. So they do not appear in the
Inspect viewer, their model calls are not in the eval log or the audit budget,
they cannot be recomputed with `inspect score`, and the code needs `_fresh()`
because Inspect's cached clients belong to an event loop that has already
closed.

The probe's apparent advantages (asking the model something new, leaving out
auditor-planted content, looking at branches) do not need code outside the
scorer system. The real gap is plumbing: the authorship labels
(`AuditController.authors()`) and each agent's final model request exist only
on in-memory controllers, and `_adopt` keeps only the final branch's store.

## Goal and acceptance

Stage 1 of issue #19. Every measurement `measure()` makes becomes an Inspect
scorer, so its result is in the eval log and the viewer and can be rescored.
`results.json` and `report.py` keep working unchanged.

- Each model-based measurement is its own scorer: `probe`, `post_judge`,
  `verdict` (release_pressure only), `free_probes`. The deterministic ones
  (mentions, exposure, copying, plan check) share one model-free `counts`
  scorer.
- Judge and probe models come from Inspect model roles, so
  `inspect score --model-role judge=…` rescores with a different judge without
  re-running the agents.
- The authorship labels and each agent's final request are written to the
  sample's store for every branch, abandoned ones included.
- `measure()` makes no model calls. For every scenario and condition under
  `--mock` it rebuilds rows identical (except `seconds` and `log`) to those
  produced by the current code.

Out of scope (later stages of #19): dropping `results.json`, a log-reading
`report.py`, removing `reverdict.py`, interventions and auditor stats from log
events, auditor token counts, the condition label in task metadata.

## What the spike established (throwaway probe, not kept)

Scripted mock models only; a baseline `mfa_rumor`, an L0 `mfa_rumor`, and L1
`kestrel_naming` with one rollback forking at revisions 2, 6 and 9.

- Orbit's `model_exposure` events (`inspect_model_event_id`) are **not** a
  generation-to-event map. They follow delivered messages: in the baseline Dave
  generated three times and has none.
- Every target `ModelEvent`'s span chain names its agent and branch
  (`…agent:dave → … → branch:branch-0`). The auditor's own model calls nest
  under the agent's span too (an `orbit_audit.auditor` span); unfiltered, Bob
  showed 6 events for 3 generations.
- With auditor spans excluded, an agent's generations in a branch map 1:1 to
  its target `ModelEvent`s, except for generations replayed from the parent.
  At revision 9, `branch-1` replayed Bob's first generation: trace input sizes
  `[3,5,7]` against event input sizes `[5,7]`. Replayed generations are a
  prefix, so the live events align with the later generation indices.
- Not tested: `edit_generation`, real models. Replaced generations (the seat's)
  have no target `ModelEvent`; the seat is never probed.

## Design

### Placement

- `src/orbit_audit/measurements.py` (new): collects each branch's record and
  holds the resolution rule. Stateless functions plus a small recorder the
  runner owns.
- `src/orbit_audit/runner.py`: after each branch finishes, asks the recorder to
  record it; `_adopt` writes the result to the store beside `BRANCHES_KEY` and
  `BUDGET_KEY`. Nothing is written to the store during the run (T2: auditor-only
  material must not reach target requests).
- `experiments/scorers.py` (new): the per-scenario scorer factory. It depends
  on `Scenario`, so it stays with the experiments.
- `experiments/run.py`: `run_condition` passes the scorers to `audit_task`
  (`scorer=` reaches the `Task` unchanged) and `model_roles` to `inspect_eval`.
  `measure()` reads scores and the store instead of calling models.

### Store schema

```
orbit_audit:measurements = {
  "version": 1,                      # MEASUREMENTS_VERSION
  "branches": {
    "<branch id>": {
      "authors":  {"<message id>": "agent:bob" | "auditor-as:alice" | …},
      "generations": {"<agent>": <count>},
      "requests": {"<agent>": {"event": "<ModelEvent uuid>" | null,
                               "via": "<branch id>" | null,
                               "why": "<reason when event is null>"}},
    },
  },
}
```

- `authors` is `AuditController.authors()` at the end of the branch.
- `generations` is the number of generations each agent had in the branch
  (`controller.generations`). The `counts` scorer needs it for the plan check
  (`ran_full_schedule`: every agent got exactly its scheduled activations),
  and the model-free scorer has no controller.
- `requests` holds each agent's final model request, one entry per agent that
  generated in the branch.
- The key is absent when a sample-level limit stops the run before `_adopt`.
  Scorers return an incomplete score, never zero.

### Resolving an agent's final request

At the end of branch B, for each agent A with generations in B:

1. Candidates: target `ModelEvent`s in `transcript().events` whose span chain
   has `agent:A` and `branch:B` and no `orbit_audit.auditor` span, in order.
2. If there is at least one, the last is A's final request; `via` is B.
3. Otherwise (all A's generations in B were replayed) use B's parent's
   resolved entry for A; `via` is that branch. The parent finished first, so it
   is already resolved. Replay reproduces the parent's request (R7).
4. If neither exists, `event` is `null` with a `why`.

The rule lives in one function and is tested on its own.

### Scorers

`measurement_scorers(scenario)` returns the list for `scorer=`. Each scorer
returns `Score(value={agent: scalar, …}, metadata={"branches": {…}})`: the value
is the final branch's, which the viewer shows; the metadata carries the same
measurement for every branch (this replaces `branch_results`).

| Scorer | Model | Replaces |
|---|---|---|
| `probe` | agent's own (`spec.model`, else the task model) | `probe`, `adopted` |
| `post_judge` | role `judge` | `flags`, `flagged_posts`, `outcome` |
| `verdict` | role `judge` | `verdict`, `reviewer_said` |
| `free_probes` | agent's own | `free_probes` answers |
| `counts` | none | `exposure`, `mentions`, `own_mentions`, `copied*`, `mention_channels`, `plan` |

- `outcome` is computed inside `post_judge`: `scenario.outcome(store, agent,
  flags)` takes the post judge's flags (they decide `disclosed` in
  `skipped_tests`), and scorers cannot rely on each other's results.
- An agent's context for a probe is the `ModelEvent.input` named by its
  `requests` entry, resolved from the transcript. This works live and under
  `inspect score`, which seeds the transcript with the sample's events.
- A model call that raises, or an entry with `event: null`, yields an
  incomplete score for that agent (`""` in the value, the reason in metadata);
  it never fails the sample. `Score.value` cannot hold `None`.
- The reviewer's last message and the final completion for `verdict` come from
  the store and `sample.output`, as today.

### `measure()` after stage 1

Reads `sample.scores`, the store and the controllers, and rebuilds the existing
row shape (`agents[a]`, `verdict`, `branch_results`, …). Fields that combine
two scorers' results are derived here, not in a scorer: each free probe's
`uses` (does the answer use the name) and `spontaneous` (a probe other than
`recall` used it, or the agent used it in its own uncopied posts). Interventions,
auditor stats and `seat_posts` still come from the controllers. `_fresh()` is
no longer needed for judge and probe calls.

## Testing

1. **Golden rows, recorded first.** Before any code change, run `--mock` for
   all six scenarios under baseline, L1 and L0 on `main` and save the rows
   (without `seconds` and `log`) as test fixtures. First confirm two runs agree.
   The new `measure()` must reproduce them exactly.
2. **Resolution rule:** plain, replayed prefix (the revision-9 case), fully
   replayed (parent fallback), unresolvable (`null` with a reason).
3. **Rescoring:** run `score_async` over a saved mock log with a different
   `judge` role; scores change and the agents are not re-run.
4. **Failure paths:** a raising model call gives an incomplete score and a
   successful sample; a missing `orbit_audit:measurements` gives incomplete.
5. The existing e2e tests pass unchanged.

## Risks and open questions

- Verified against mock models only. A live run (OpenRouter) is a follow-up
  for the person with the key.
- `edit_generation` is untested for the resolution rule; the plan adds a case.
- Whether Inspect keeps a custom scorer list together with Orbit defaults is
  not checked; no scenario sets `scorer` today, so the plan verifies what the
  task scores with and without.
- Inspect's role-resolution under `inspect score` was read in source
  (`_eval/score.py`), not run with a custom scorer; the rescoring test covers it.
- Reading `transcript().events` once per branch is a scan of the whole
  sample; fine at seed-audit scale.
