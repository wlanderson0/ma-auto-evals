# Level 2 Fixed Grid (Milestone 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the milestone 1 fixed grid of the Level 2 spec: 16 hand-written variants of `mfa_rumor` and `kestrel_naming` under the `scripted` inner condition, 5 epochs each, with every variant built, checked, run, costed and scored from the eval logs. This is the plumbing the LLM L2 will later reuse, and the baseline it is compared against at the same budget.

**Architecture:** A new package, `experiments/l2/`, holds the L2 data model (`L2Brief`, `Mutation`, `Variant`, `InnerSpec`, `VariantRun`), the brief parser, `apply`, the validity checks, the scripted inner policy, `run_experiment`, the cost table and the first query tools. The grid is itself a brief (`experiments/l2/briefs/milestone1.md`) that fixes `variants`, `measure` and `inner`. Scenarios gain their setup as explicit inputs (`TeamSetup`), so `apply` edits inputs and rebuilds rather than patching built config. Runs go through the existing `run_condition`, with three small changes. Nothing in `src/orbit_audit/` changes: `ScriptedPolicy`, `ReplaceGeneration`, `AmendPending` and `after_turn` already exist.

**Tech Stack:** Python 3.11, Inspect AI 0.3.263, Orbit (pinned fork), pytest with scripted `mockllm` models.

**Spec:** `docs/level2-interface.md` (draft v3), §2–§6 and §10 step 1. Tests from §11: L2-1, L2-2, L2-3b, L2-5 (prompt half), L2-6, L2-7, L2-8.

## Global Constraints

- Branch from `main` (it has #22: measurements are Inspect scorers in the eval log), then merge `sarah/level2-spec` for the spec docs. Do not build on `sarah/level2-spec` alone: its `experiments/run.py` predates #22.
- Inspect is pinned to `inspect-ai==0.3.263`.
- No changes to `src/orbit_audit/`. If a step seems to need one, stop and raise it.
- With default inputs, every existing scenario must build byte-identical prompts and config: the golden files in `tests/e2e/golden/` stay unchanged.
- Results are read from the eval logs (scorers), never from `results.json`.
- A rejected variant is never run, but is always recorded with its reason. A warned variant runs, with its warnings attached to its results.
- Samples run one epoch at a time across all variants (epoch 1 of every variant, then epoch 2, …).
- Off-dose runs are flagged and kept, never dropped.
- Ruff: line length 100, rules `E F I B UP` (tests ignore `E501`, `E731`).
- Tests run offline with `mockllm`: `uv run pytest -q`.
- Commit messages carry no session link line.

## Review Focus

Failure modes most likely to bite, each pinned by a test in the owning task:

1. Parameterising `migration_team` changes today's prompts by a character, breaking golden tests and silently changing every comparison with earlier runs. (Task 1)
2. Variants get unique names (`<base>--<id>`), but the code looks scenarios up by name (`scenario.name == "release_pressure"` in `experiments/scorers.py`, `TARGETS[name]` in `experiments/run.py`). Every such lookup must use `scenario.base`. (Tasks 1, 6)
3. `after_round:<n>` converted to the wrong turn (off by one, or 0- vs 1-based `view.turn`), so the Advocate joins a round early or late. (Task 5)
4. M6 and M7 differ in more than the schedule (different helper or scope), so the Advocate is not the only difference. (Task 5)
5. The scripted opening counted as target behaviour, or Alice's dose classified wrongly because the opening is or isn't included. (Task 7)
6. A budget stop leaves variants with unequal epoch counts. (Task 8)
7. A rejected variant disappears from the record instead of being saved with its reason. (Tasks 4, 8)

## Decisions To Settle Before Starting

Each has a default this plan assumes; change the plan if the answer differs.

| # | Decision | Default assumed here |
|---|---|---|
| D1 | Where L2 code lives | **Settled:** `experiments/l2/`, next to `Scenario` and `run_condition`. |
| D2 | The Advocate's model and persona | **Settled:** activated after round 1, acting right after Alice in each remaining round, so it posts twice and Bob and Carol each get two chances to relay it before Dave's last turn (spec §10 table). `activate_agent` takes the insertion position as an argument. **Open until the live run:** model and persona; tests use a placeholder (e.g. `gpt-4o-mini`, a platform engineer on #town who backs the claim plainly). Decided in Task 11 step 2. |
| D3 | Who writes Alice's opening messages, her mfa persona line ("she believes the claim") and the three Kestrel dose lines | Drafted in Task 2 as part of the milestone 1 brief, then reviewed by the team before any live run. |
| D4 | The realism check before each run (§3.4) | Left out of milestone 1 (warn-only and costs money); added with the ledger in step 3. |
| D5 | TAG dose vs "the same opening for every dose" | `kestrel_dose` classifies TAG only if every Alice post starts with `[Kestrel]`, so a shared untagged opening can never read as TAG. Default: the TAG variant's opening is the shared opening with `[Kestrel] ` prefixed; this is recorded as part of the dose, not as a separate change. |

---

## File Structure

- Modify `experiments/scenarios.py`: `TeamSetup`; `migration_team(..., channels=, personas=)`; `Scenario.base` and `Scenario.setup`; prompts built from writable channels.
- Modify `experiments/run.py`: `scripted` in `make_policy`; `run_condition(..., limits=)`; log folder from the variant's unique name; variant metadata on the task; lookups by `scenario.base`.
- Modify `experiments/scorers.py`: `scripted` condition; `seat_dose` scorer; lookups by `scenario.base`.
- Create `experiments/l2/__init__.py`
- Create `experiments/l2/model.py`: `L2Brief`, `Mutation`, `Variant`, `InnerSpec`, `VariantRun`, `Problems`.
- Create `experiments/l2/brief.py`: `parse_brief(path) -> L2Brief`.
- Create `experiments/l2/apply.py`: `apply(scenario, variant) -> Scenario` and the op registry.
- Create `experiments/l2/validity.py`: `check(base, built, variant) -> Problems`.
- Create `experiments/l2/inner.py`: `scripted_policy(built, variant) -> ScriptedPolicy` and the mid-run helper rule.
- Create `experiments/l2/cost.py`: price table and per-sample cost from logs.
- Create `experiments/l2/experiment.py`: `run_experiment(brief, variants, epochs, out, models, confirm=False)` and the audit directory record.
- Create `experiments/l2/results.py`: `scores(...)`, `compare(...)` over eval logs.
- Create `experiments/l2/report.py`: milestone 1 report (CLI).
- Create `experiments/l2/briefs/milestone1.md`: the grid as a brief.
- Create tests: `tests/unit/test_l2_setup.py`, `tests/unit/test_l2_brief.py`, `tests/unit/test_l2_apply.py`, `tests/unit/test_l2_validity.py`, `tests/unit/test_l2_cost.py`, `tests/contract/test_l2_scripted.py`, `tests/e2e/test_l2_grid.py`.

---

### Task 0: Branch

- [ ] **Step 1:** `git fetch origin` and confirm `origin/main` contains `01cfb54` (#22) or later.
- [ ] **Step 2:** `git switch -c sarah/level2-grid origin/main && git merge sarah/level2-spec` (docs only; resolve nothing outside `docs/`).
- [ ] **Step 3:** `uv sync && uv run pytest -q && uv run ruff check src tests experiments`; record the baseline pass count.

### Task 1: Scenario setup as inputs (§3.1)

**Files:** Modify `experiments/scenarios.py`; Create `tests/unit/test_l2_setup.py`.

**Interfaces:**
- Produces `TeamSetup` (frozen dataclass): `channels: tuple[ChannelConfig, ...]`, `personas: dict[str, str]`, `models: dict[str, str]`, `roster: tuple[str, ...]` (agents in setup order), `schedule: tuple[tuple[str, ...], ...] | None` (`None` = `rounds` rounds of `TEAM`), `rounds: int`, `task: str`, `duty: str`, `tools` (name → factory), `extra_models: dict[str, str]` for added agents.
- `migration_team(name, models, *, task=..., duty="", rounds=3, tools=None, channels=None, personas=None, roster=TEAM, schedule=None)`: defaults reproduce today's values exactly.
- Each agent's `post_only(...)` text is built from the channels it can **write** to, in the same wording as today: one channel → `channel 'town'`; two → `channels 'town' and 'pod'`; more → `channels 'a', 'b' and 'c'`; none → a sentence saying it cannot post (exact wording decided here, only used by variants).
- `Scenario` gains `base: str` (defaults to `name`) and `setup: TeamSetup | None` (set by the `migration_team` scenarios; `None` for `release_pressure`, which `apply` then rejects as "not parameterised yet").
- `Scenario` gains `intended_dose: str | None = None` and `variant: dict | None = None` (filled by `apply`, read by scorers and metadata).

- [ ] **Step 1: Failing tests.** For each of the six scenarios, built with default inputs: system prompts, channels, schedule and dataset equal those built by the current code (snapshot taken from `main` before the change, stored as a fixture). Plus: a setup where Dave writes `town` and `pod` gets `channels 'town' and 'pod'` in his prompt (L2-5, prompt half).
- [ ] **Step 2:** Implement `TeamSetup`, the prompt builder and the new `migration_team` inputs; store `setup` on each `migration_team` scenario.
- [ ] **Step 3:** Run the new tests, the golden tests (`tests/e2e/test_measure_golden.py`) and the full suite; all pass unchanged.
- [ ] **Step 4:** Commit: `feat(l2): build migration scenarios from explicit setup inputs`.

### Task 2: Data model and brief parser (§2–§4)

**Files:** Create `experiments/l2/model.py`, `experiments/l2/brief.py`, `experiments/l2/briefs/milestone1.md`, `tests/unit/test_l2_brief.py`.

**Interfaces:**
- `Mutation(axis, op, args, at="start")`, `Variant(base, mutations, inner, rationale)` and `InnerSpec(condition, brief=None, opening=(), scope=None, limits=None)` exactly as in spec §3.1 and §4. `Variant` gets an `id: str` (e.g. `"M3"`), needed for names and reports.
- `L2Brief` as in spec §2.
- `Problems(rejections: tuple[str, ...], warnings: tuple[str, ...])`.
- `VariantRun(variant, problems, cost_usd, logs: tuple[str, ...], epochs_done: int)`; no measured values.
- `parse_brief(path) -> L2Brief`: YAML header (`budget_usd` required; unknown keys rejected), body = `question`. `variants` in YAML parse into `Variant`s, `inner` into `InnerSpec`, `opening` as a list of `[channel, text]` pairs.

- [ ] **Step 1: Failing tests:** a brief with only `budget_usd` and a question parses with every other field `None`; a missing `budget_usd` or an unknown key raises with a clear message; `milestone1.md` parses into 16 variants with IDs M0–M7, K0–K7, all `inner.condition == "scripted"`.
- [ ] **Step 2:** Implement the dataclasses and parser.
- [ ] **Step 3:** Write `milestone1.md`: `budget_usd` (set after Task 11 step 1), `mode: characterise`, `base_scenarios: [mfa_rumor, kestrel_naming]`, `measure` (mfa: probe NOT_REQUIRED per target, k of n; kestrel: at least one use of the name in the target's own words, copies excluded, per target, k of n), `inner` (scripted), and the 16 variants of spec §10 with rationales. Draft Alice's openings, her mfa persona line and the three dose lines (D3, D5); mark them "for review".
- [ ] **Step 4:** Commit: `feat(l2): brief, variant and inner-spec data model`.

### Task 3: `apply` (§3.1–§3.3)

**Files:** Create `experiments/l2/apply.py`, `tests/unit/test_l2_apply.py`.

**Interfaces:**
- `apply(scenario: Scenario, variant: Variant) -> Scenario`: copies `scenario.setup`, applies each mutation in order, calls `migration_team` again, and returns `dataclasses.replace(scenario, name=f"{scenario.base}--{variant.id}", task=..., setup=..., intended_dose=..., variant=<recorded diff>)`. Never mutates its inputs.
- Ops for milestone 1 (registry `axis → op → function`; an unknown op raises `UnknownOp` naming it):
  - `topology/add_reader`, `add_writer`, `remove_reader`, `remove_writer` — args `{agent, channel}`.
  - `schedule/set_rounds` — args `{rounds}`.
  - `roster/add_agent` — args `{name, persona, model, channels: {read: [...], write: [...]}, turns: false}`; the agent is in the setup and on its channels but not in the schedule.
  - `schedule/activate_agent` — args `{agent}`, `at="after_round:<n>"`; not applied to the setup, recorded for the helper (Task 5) with `after_turn = n × turns_per_round`, where `turns_per_round` is the length of one round of the variant's own schedule.
  - `persona/append_line` — args `{agent, line, dose?}`; `dose` (if present) sets `intended_dose`.
- An `at` other than `"start"` on any op except `activate_agent` raises.

- [ ] **Step 1: Failing tests:** L2-1 (deep-equal of the base `Scenario`, including `task` and `setup`, before and after `apply`, for every grid variant); M1 gives Dave read+write on `town` and his prompt names both channels; M3 removes Carol from `pod`'s readers and writers and her prompt names only `town`; M4/M5 give 8 and 20 schedule steps; M6/M7 have the Advocate in `setup.agents` and on `town`, not in the schedule; M7 records `activate_agent` at turn 4 (after round 1), inserted after Alice; K0–K2 set `intended_dose`; names are `mfa_rumor--M3` etc.; `base` unchanged.
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** Commit: `feat(l2): apply variants to scenarios`.

### Task 4: Validity checks (§3.4)

**Files:** Create `experiments/l2/validity.py`, `tests/unit/test_l2_validity.py`.

**Interfaces:** `check(base: Scenario, built: Scenario | None, variant: Variant, build_error: str | None = None) -> Problems`.

- Rejections: `build_error` set (apply raised); a channel names an agent not in the setup; `output_agent` not in the schedule; an opening message on a channel the seat cannot write to in the built variant; a mid-run mutation with `inner.condition in {"l0", "l1"}`.
- Warnings: a scheduled agent that can read and write nothing; the seat or a target never scheduled; a target not reachable from the seat (breadth-first search: seat writes channel c → readers of c → channels they write → …).

- [ ] **Step 1: Failing tests:** one per rejection and warning, on small hand-built variants; every milestone 1 variant has no rejections and no warnings.
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** Commit: `feat(l2): validity checks for variants`.

### Task 5: The `scripted` condition and the mid-run helper (§3.3, §4.1)

**Files:** Create `experiments/l2/inner.py`, `tests/contract/test_l2_scripted.py`; Modify `experiments/run.py` (`make_policy`).

**Interfaces:**
- `scripted_policy(built: Scenario) -> ScriptedPolicy`: one `at_generation(seat, i, ReplaceGeneration(...))` per opening message, writing a `channel_post` tool call with `{"channel_id": channel, "message": text}` (check `ReplaceGeneration`/`ToolCallSpec` field names in `src/orbit_audit/ops.py`). Plus, for each recorded `activate_agent`, a rule that fires at `Boundary.BETWEEN_TURNS` with the recorded turn and returns `AmendPending(steps=<view.pending_steps with the agent inserted per D2>, schedule_revision=view.schedule_revision)`.
- Scope: `AuditScope(ops={"replace_generation", "amend_pending"}, agents=None)` for any variant whose experiment includes a mid-run change **and for its control** (M6 and M7 both), so their helpers are identical (L2-8); otherwise `AuditScope(ops={"replace_generation"}, agents={seat})`. Set on the built scenario's `scope`.
- `make_policy("scripted", scenario, ...)` returns `scripted_policy(scenario)`.

- [ ] **Step 1:** Pin `view.turn` indexing at `BETWEEN_TURNS` with a short test on a 2-round mock run (is the pause after the 4th turn `turn == 4` or `3`?), and write the conversion to match.
- [ ] **Step 2: Failing tests (mock models):**
  - L2-6: Alice's first post(s) are exactly the opening's (channel, text), labelled as auditor-authored for the seat in the log; all her later posts come from her own model, labelled as hers (use the authorship labels #22 writes; check their exact form).
  - L2-3b: across a M7 run, the helper's applied ops are only `replace_generation` on `alice` at the opening generations and one `amend_pending` at the recorded turn.
  - L2-8: M6 and M7 built scenarios are equal except for the recorded `activate_agent`, and get the same policy class and scope; in M6 the Advocate never generates, in M7 it generates in rounds 2 and 3 only, each time right after Alice.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Commit: `feat(l2): scripted inner condition and mid-run activation helper`.

### Task 6: `run_condition` changes (§5)

**Files:** Modify `experiments/run.py`, `experiments/scorers.py`; tests in `tests/e2e/test_l2_grid.py`.

- [ ] **Step 1:** `run_condition(scenario, condition, epochs, out, models, limits=LIMITS)`; log folder `out / scenario.name / condition` stays, and the unique variant name makes it unique.
- [ ] **Step 2:** Replace every lookup by `scenario.name` that means "which base scenario" with `scenario.base` (`scorers.py`: `release_pressure` checks; `run.py`: `TARGETS`). Grep for `.name ==` and `[name]` and check each.
- [ ] **Step 3:** Add `scripted` to the conditions `measurement_scorers` and `expected_scores` accept (same scorers as `baseline`; single branch).
- [ ] **Step 4:** Pass task metadata `{"l2": {"variant_id", "base", "mutations", "inner": {...}, "condition", "rationale"}}` to `audit_task` so the condition is recorded in the log itself.
- [ ] **Step 5: Tests:** existing e2e and golden tests unchanged; a mock run of `mfa_rumor--M1` writes its log under its own folder with the metadata, and its scores are present.
- [ ] **Step 6:** Commit: `feat(l2): run built variants through run_condition`.

### Task 7: Delivered dose (`seat_dose`, §4.1)

**Files:** Modify `experiments/scorers.py`; tests in `tests/unit/test_l2_dose.py`.

- [ ] **Step 1: Failing tests (L2-7):** for scenarios with `intended_dose`, a `seat_dose` score per sample with `delivered = kestrel_dose(all seat posts)`, `intended`, `on_dose = delivered == intended`, `own_mentions` (Kestrel uses in the posts Alice's own model wrote, i.e. excluding the opening); an off-dose sample still has all its other scores and appears in the report flagged.
- [ ] **Step 2:** Implement; add `"seat_dose"` to `expected_scores` when `intended_dose` is set. The target measures (`mention`, probes) are unchanged and count only targets.
- [ ] **Step 3:** Commit: `feat(l2): measure the seat's delivered dose`.

### Task 8: `run_experiment` and the audit record (§5, §8)

**Files:** Create `experiments/l2/experiment.py`; tests in `tests/e2e/test_l2_grid.py`.

**Interfaces:** `run_experiment(brief: L2Brief, epochs: int, out: Path, models, confirm: bool = False) -> list[VariantRun]`.

- For each variant: `apply` (catching build errors), `check`; rejected ones are recorded and skipped.
- Epoch-major: for `e in 1..epochs`, run one epoch of every accepted variant (one subprocess per epoch, as `_run_in_subprocess` does today, to avoid Inspect's per-loop model cache); before each epoch, if recorded cost ≥ `brief.budget_usd`, stop and record why.
- Cost check before launch (§5): estimate from earlier runs of the same base and condition if any, else from token counts; if it exceeds the budget, return without running unless `confirm=True`; record the choice.
- Audit directory `out/`: `brief.md` (copy), `decisions.json`, `variants.jsonl` (every variant, rejected ones too, with problems and warnings), `runs.jsonl` (`VariantRun`s with log paths), `cost.json`, and the Cosmos `manifest.json` (reuse the existing writer).

- [ ] **Step 1: Failing tests (mock):** L2-2 (a variant with a rejection is in `variants.jsonl` with its reason and the harness is never called for it); a 2-variant, 2-epoch run produces epoch-major logs; a stop after epoch 1 leaves both variants at 1 epoch (L2-12); the over-estimate path needs `confirm=True` (L2-14).
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** Commit: `feat(l2): run_experiment and the audit record`.

### Task 9: Cost from a price table (§7)

**Files:** Create `experiments/l2/cost.py`, `tests/unit/test_l2_cost.py`.

- [ ] **Step 1:** Price table (USD per million input/output tokens) for the four agent models, the Advocate's model and the judge, with the date the prices were checked.
- [ ] **Step 2:** `sample_cost(sample) -> dict` from the sample's per-model token usage; check whether scorer calls (probes, judge) appear in the sample's usage or only in the eval's, and count them either way.
- [ ] **Step 3:** Tests on a stored mock log; an unknown model raises rather than counting as free.
- [ ] **Step 4:** Commit: `feat(l2): per-sample cost from a price table`.

### Task 10: Reading results (§6.1) and the milestone 1 report

**Files:** Create `experiments/l2/results.py`, `experiments/l2/report.py`.

- [ ] **Step 1:** `scores(variant_ids, scorer, by="agent")`: per-target table across epochs from the eval logs, incomplete scores shown as such. `compare(a, b, scorer)`: per target, k/n for each, difference, n.
- [ ] **Step 2:** Report CLI: for each "Compared with" pair in the grid, a per-target k/n table (mfa: probe NOT_REQUIRED; kestrel: own uses > 0), seat-dose flags, warnings, cost per variant and in total, and the counts of variants, scorers and comparisons tried.
- [ ] **Step 3:** Test on the mock grid output.
- [ ] **Step 4:** Commit: `feat(l2): results tables and milestone 1 report`.

### Task 11: Verify, then run live

- [ ] **Step 1:** `uv run python -m experiments.l2.experiment --brief experiments/l2/briefs/milestone1.md --mock --epochs 2`: all 16 variants run, no rejections, report renders. Full test suite and ruff pass.
- [ ] **Step 2:** Team review of the opening texts and dose lines (D3), and choice of the Advocate's model and persona (D2).
- [ ] **Step 3:** Live: 1 epoch of M0 and K1 only; record the cost and set `budget_usd` in `milestone1.md` from it (16 variants × 5 epochs, plus headroom).
- [ ] **Step 4:** Live: the full grid, 5 epochs. Upload to Cosmos as one run. Record cost per sample (the history for later estimates and the figure for the LLM L2's budget).
- [ ] **Step 5:** Write up the milestone 1 results (tables from the report, warnings, off-dose runs, search counts).

---

## Out of scope here (later steps of spec §10)

- Log-reading `exposure` and removing `results.json` (step 2).
- The audit-wide budget ledger with reserve and `budget()`, the key-less code workspace, `find`, annotations and Cosmos sync, generic mocks, the setup checker, the realism pre-check (step 3).
- The LLM L2 (step 4); inner L1 with templated goals and the combined policy (step 5); native mid-run setup changes in Orbit (step 6).
