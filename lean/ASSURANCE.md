# Assurance statement: audit scope and rollback

**Pinned to:** branch `sarah/lean-scope`: the Python of `src/orbit_audit/` with the G1–G3 and RB2 fixes and D5, and the specification and proofs updated with them; Lean `v4.35.0`, core and `Std` only; Orbit fork `Danhelo/orbit@dc4ea5e`. Specification: `lean/Scope/Spec/*.lean`, listed in `lean/Scope/Spec/SHA256SUMS` (sha256 `b0720ef581c891f48117be51f75102da2557c5cb2d530624f012b447c0ca4f67`). Gate: `lean/check.sh` and the files it runs, listed in `lean/GATE.SHA256SUMS` (sha256 `0fbfe89da3333863f213a85e3695abaa4895aa7c207aaa2b18d8eb4285c59617`).

**Status:** the scope statements have had three adversarial reviews by AI agents, the rollback statements (`Branch*.lean`) two, and the rollback model one. None has had the **human review** the plans require before the claim is relied on. The rollback statements were revised while they were proved, and first checksummed with their proofs (commits `9992a5b`, `6f41a42`): review them as a whole.

Two parts: **audit scope** (below, plan `docs/superpowers/plans/2026-11-09-lean-scope.md`) and **rollback** (section "Rollback", plan `docs/superpowers/plans/2026-11-17-lean-rollback.md`, code map `lean/rollback-map.md`).

## The claim

> `Scope.scopeRespected`: in every run of the model of an audit under an `AuditScope s`, every op the controller applies was checked against `s` in the world it changed. And every applied op other than `halt` and `rollback` leaves each agent outside `s`'s agent limit seeing exactly what it saw before. This is proved about the Lean model. That the model agrees with the Python is tested, not proved.

"Seeing" means the specification `view` (`View.lean`):
- the agent's own generations, staged context and tool results;
- the channel messages it is shown, after the delivery overlay;
- its pending turns and their positions;
- whether the run is halted;
- the words the overlay attributes to it (the current rewrite of each of its messages, and injections sent as it).

## Limits of the claim, up front

- **G1 and G2 are fixed**, so the frame (`Frame`) holds with no gap allowance.
- **`halt` and `rollback`** are exempt by name in the scope model. A rollback's own effect, and whether a fork reproduces its parent's world, are stated in the rollback model (`Fidelity`, `ThroughFork`, `ForkEffect`), under T8 and `NotNestedAbove`; see "Rollback".
- **Only the immediate effect of each op.** The frame covers what an op changes at once. Words an op causes to be posted later are not covered: a seat's generation that posts to a channel, or a forged `channel_read` result.
- **Unique message ids.** The frame assumes them (`World.WF`). The model does not derive this for the worlds a run reaches, because the steps between boundaries are arbitrary.
- **Property 5 holds by construction in the model.** At the Python level it rests on T1 and T2.

## What is proved

| # | English | Lean statement | Proof | Python | Tied to the Python by | Witness |
|---|---|---|---|---|---|---|
| 1 | A clean scope check means the op name and every affected agent are allowed, and so is each rollback intervention | `ScopeSound` | `scopeSound` | `scope.py:52-71` | `test_problem_and_affected`, saved examples | `AgentsMutantWitness` |
| 2 | An op that passes the controller's other checks and applies leaves every agent outside `affected` unchanged | `Frame` | `frame` | `ops.py`, `controller.py:198-298`, `delivery.py:37-58` | `test_message_op_effects`, `test_turn_op_effects`, `test_frame_on_python_affected` | `FrameWitness`, `SeatWitness`, `HaltWitness` |
| 2′ | G1 fixed: `amend_pending` counts every agent whose pending turns it replaces | (part of `Frame`) | `frame` | `ops.py:419-423`, `controller.py` `_pending` | `test_g1_*` in `tests/contract/test_scope_gaps.py`; differential tests with the schedule passed | `G1Witness` |
| 2″ | G2 fixed: an op naming no agent is refused, and one accepted at an agent's boundary names exactly that agent | `G2Fixed` | `g2Fixed` | `ops.py:95-96, 189-190, 221-222`, `controller.py:536-541` | `test_g2_*` in `tests/contract/test_scope_gaps.py` and `tests/lean/test_differential.py` | — |
| 3 | Every op an audit applies, live, replayed or at a fork, passed the scope check in the world it changed | `Enforced` | `enforced` | `controller.py:412-541`, `runner.py:99-147` | live batches: `test_controller_problem`, `test_decide`; replay and fork paths: `tests/lean/test_rollback_agreement.py` (batches, desyncs, ends; see "Rollback"); AST inventory (T1) | `RunWitness`, `ReplayMutantWitness` |
| 4a | Widening a scope never rejects more | `Monotone` | `monotone` | `scope.py:49-71` | — | `MonotoneWitness` |
| 4b | The meet of two scopes allows exactly what both allow (no Python counterpart yet) | `Meet` | `meet` | — | — | `MeetWitness` |
| 4c | At a boundary that does not roll back, the world changes only through the ops applied there | `OnlyApplied` | `onlyApplied` | `controller.py:412-458` | `test_decide` | `OnlyAppliedWitness` |
| 5 | Every applied op was checked against the audit's own scope | `Confined` | `confined` | `runner.py:119-123` | T1, T2 | — |
| H | The headline: 1–3 and 5 | `ScopeRespected` | `scopeRespected` | all of the above | all of the above | `RunWitness` |

- **Axioms.** Every proof depends only on `propext`, `Classical.choice` and `Quot.sound` (some witnesses on `propext` alone). `leanchecker` re-checks the 19 `Scope.*` modules.
- **Size.** 146 proved theorems (lemmas included; 71 for rollback) for 21 statements and 19 frozen witness statements, all 40 pinned in `Axioms.lean`.
- **Representation witnesses** (`RepresentationWitness`): `none` vs `some []` for scope limits and for message recipients, and nested rollbacks.
- **The gate** (`check.sh`) fails if:
  - the specification or the gate changes without a new checksum;
  - a proof file imports anything but `Scope.*`;
  - a proof file uses one of a list of constructs that can add axioms or run code;
  - a specification file is not listed in its checksum file;
  - a statement or witness (an argument-free `def X : Prop` in the specification) is not pinned in `Axioms.lean`, or has no proof of exactly its type;
  - an axiom is outside the allow-list;
  - there is any warning.

### Traceability

| Statement | Specification definitions it rests on | Axioms | Statement reviewer |
|---|---|---|---|
| `ScopeSound` | `AuditScope.problem`, `Op.affected`, `Op.interventions` | propext, Classical.choice, Quot.sound | AI only |
| `Frame` | `applyRaw`, `checkProblem`, `view`, `carvedOut`, `World.WF` | same | AI only |
| `Enforced` | `runAudit`, `decide`, `rollbackProblem`, `AuditScope.problem` | same | AI only |
| `Monotone`, `Meet` | `AuditScope.Le`, `AuditScope.meet`, `AuditScope.problem` | same | AI only |
| `OnlyApplied` | `decide`, `Chain` | same | AI only |
| `Confined` | `runAudit` | same | AI only |
| `ScopeRespected` | all of the above | same | AI only |

## How the model is tied to the Python

- **Source references.** Every mirror definition cites the Python lines it follows. `tests/structural/test_lean_mirrors.py` checks that each of the 106 references is in range, and that the 37 that name a function or class still name a `def`/`class` there. The citations in this file are not checked.
- **Differential tests** (`tests/lean/`) run the Lean model as a subprocess against the real code:
  - 6 property tests, at 1000 examples each on a PR and 5000 nightly;
  - 22 saved examples covering the gaps, the representation cases and the batch and rollback rules;
  - codec tests (`null` vs `[]` vs a missing field, duplicate keys, any Unicode);
  - a check that every registered op has a Lean counterpart.

  The tests take about 45 seconds locally.
- **Mutation adequacy.** 8 mutants of the Python are each caught:
  - the scope check ignoring agents;
  - `affected` ignoring implicit recipients;
  - no targeted check;
  - delivery ignoring per-recipient drops;
  - no sole-producer rule;
  - rollback skipping the scope check;
  - `amend_pending` counting only its new steps (G1);
  - `[]` read as `None`.

  The plan's D1 and replay-skips-the-check mutants are not among them (the replay mutant is a Lean witness). An earlier review ran 20 mutants by hand. Two survived 300 examples but were killed at 3000: an injection ignoring routes, and one ignoring readers. One was never killed: delivery not recording `delivered`.
- **Inventory.** `tests/structural/test_scope_inventory.py` lists all 24 sites that apply an op or write delivery, schedule, scope or message state (T1).
- **CI.** `.github/workflows/lean.yml` is meant to run the gate and these tests on changes to the Python, the experiments, the Lean files or the lockfile, with a longer run nightly, and to treat missing Lean as an error. It has not run on GitHub yet.

## What is assumed

- **A1–A7** of `lean/scope-map.md`: Orbit's read path, rewrite checks and `amend_pending` semantics; the check world being the apply world; one agent at a time; no `after_activation` callback; the 13 registered ops. A1 needs observation mode `none` and no attack injections.
- **T1.** No code outside the apply sites changes the world. This holds by convention only: a Python policy can reach the live controller through `BranchQueries` or `on_controller`.
- **T2.** Policy code does not change the scope or the controller's reference to it.
- **T3.** Op values are not mutated between check and apply.
- **T4.** The Python agrees with the model. This is tested, not proved.
- **T5.** Lean's kernel and the three standard axioms are sound. The Lean interpreter and compiler are trusted for the differential driver only.
- **T6.** Results hold at the pinned commit; CI re-runs on change.

## What is not covered

- **Effects through later steps.** These need the information-flow theorem in the plan's "Later".
- **What the differential tests don't compare:**
  - the effects of generation, activation and tool ops against the Python (only checked against the Python's `affected`);
  - absolute inboxes through Orbit's own `read_messages`;
  - whole episodes (the replay and fork paths are now compared: see "Rollback");
  - a corpus replayed from real runs.
- **Not modelled:**
  - the shared message id and sequence counters, and `schedule.revision`;
  - the world between boundaries;
  - log completeness, and faithful logs or transcripts.
- **Nested scopes.** The Python gives one controller one scope.
- **Outside the model entirely:** LLM behaviour beyond "an arbitrary function", Orbit and Inspect internals beyond A1–A7, concurrency, performance and denial of service.

## Findings

Scope (`lean/scope-map.md`, "Findings (step 6)"), all fixed:
- **G1**: `amend_pending` under-reported `affected`.
- **G2**: an empty `agent` bypassed the agent limit.
- **G3**: an uncaught schedule error.

Rollback (`lean/rollback-map.md`, "Findings"):
- **RB1** (open): an accepted rollback can produce a child that rejects its interventions at the fork. Lean: `bug_ForkRejected`.
- **RB2** (fixed): a child whose run ended before its fork completed undetected. Lean: `CompletedReachedFork`, `ScoredReachedFork`; Python: `test_a_child_that_ends_before_its_fork_is_a_desync_not_a_completed_run`.
- **RB3** (open): a rollback nested past its parent's fork uses the wrong ids; it desyncs, or diverges silently. Lean: `bug_NestedDesync`, `bug_NestedSilent`, and the `NotNestedAbove` hypothesis; Python: reproduced in `tests/lean/test_rollback_agreement.py`.
- **RB4** (decided, D5): the adopted branch is always the last. Lean: `ChainShape`, `ScoredIffCompleted`.
- **RB5** (documented): fewer model calls than recorded go undetected; not modelled.
- **RB6** (open, latent): a policy raising `RollbackRequested` skips every rollback check. Lean: assumption A7 (`Policy` returns ops).

## Decisions

- **D1, decided: `halt` stays exempt by name.** It changes nothing an agent already said or saw, and the seat brief needs it to end an audit. When the auditor stops is a lever the scope cannot judge, so analyses compare variants at the rounds they share and should flag runs the auditor halted early (`lean/scope-map.md`, D1).
- **D2, decided: (a), only overlay changes count as deliveries.** Under a seat scope, a forged `channel_read` result reaches only the seat, whose inputs are not measured; it cannot change what an out-of-scope agent receives or does. If a brief's scope includes a measured agent, treat forged reads by that agent as auditor input in the analysis, and revisit (`lean/scope-map.md`, D2).

- **D4, decided: a rollback may rewind agents outside the scope.** Under an agent scope, a rollback replays every agent up to the fork, and the agents outside the scope then act live again. What they do after the fork may differ, but only through what they receive, which the scope already limits. The rollback's own effect on them is `ForkEffect`: under T8 and `NotNestedAbove`, they are at the fork in their parent's world at that revision, and, with unique message ids and no `halt` or `rollback` among the fork's ops, still see what they saw there after those ops.

- **D5, decided: a final branch that did not complete is not scored.** The adopted branch is always the last one (RB4). If it ended `replay_desync`, `budget_exhausted` or `error`, every score is incomplete and says why; the branch stays in the log as evidence (`experiments/scorers.py`, `scored_view`).

## Rollback

**The claim.** In the model of rollback (`lean/Scope/Spec/Branch.lean`), proved in `lean/Scope/Proofs/Branch*.lean`:

| # | English | Lean statement | Assumptions | Tied to the Python by | Witness |
|---|---|---|---|---|---|
| 1 | Below the fork a child is in its parent's state, applies its parent's ops (whatever the shared state) and never desyncs; it can only be cut short | `Fidelity` | T8, `NotNestedAbove` | drawn audits: same fingerprints and applied ops on real runs | `RealForkWitness`, `VetoWitness`, `SameForkWitness`, `T8Witness` |
| 1′ | It gets through its fork boundary: still running, rolled back again or cut | `ThroughFork` | T8, `NotNestedAbove` | drawn audits | same |
| 2a | A fingerprint mismatch up to the fork ends the branch as a desync with nothing applied there | `FailSafe` | none (any `Env`; the boundary is reached) | drawn audits and a not-really-pure tool: the model's `desynced` agrees | `bug_NestedDesync` |
| 2b | Every boundary up to the fork that a child got past matched the recording | `FailSafeRun` | none | as 2a | — |
| 2c | A child that completes reached its fork (RB2 fix) | `CompletedReachedFork` | none | a child halted before its fork: the model's `finished` agrees | — |
| 4 | The rollback's own effect on agents outside the scope (D4) | `ForkEffect` | T8, `NotNestedAbove`; `World.WF` and no `halt`/`rollback` at the fork for the view part | — | `ScopedForkWitness` |
| 6 | A child's id tag is 0 before its fork and its index after | `IdsOwnAfterFork` | `TagKept` | — | `bug_NestedSilent` (failure past the parent's fork) |
| S | The scope results hold for every branch, live, replayed or at a fork | `ScopeRespectedB` | `World.WF` for the view part | as the scope model | `ScopedForkWitness` |
| 7 | Branches form a chain; each child replays its parent; the scored branch is the last and only if it completed, past its fork if it is a child | `ChainShape`, `AuditLink`, `ScoredIffCompleted`, `ScoredReachedFork` | none | `tests/e2e/test_scorers.py` (D5) | `bug_NestedDesync` (scored = none) |
| 3 | An accepted rollback's interventions pass at the fork | not true: `bug_ForkRejected` (RB1) | — | drawn audits (a quarter reject at the fork) | `bug_ForkRejected` |

The plan's property 5 (a no-op rollback is the identity up to ids) is not claimed in Lean; the R7 test covers it by renumbering ids.

**Assumptions.**
- **T8.** Given the output served, Orbit's step and whether its run is complete do not depend on the branch (`Env.step`, `Env.done`). It stands for "deterministic on the branch state up to random ids" (`lean/rollback-map.md`, T8): the model's `State` holds no random ids, so equality there is that equivalence. Live outputs, limits and the shared state may differ.
- **`NotNestedAbove`.** The child's fork is not past its parent's own fork. Past it, RB3.
- **`TagKept`.** Only `diverge` changes the id tag.
- **T7 is not assumed.** The fail-safe statements hold even if fingerprints collide; collisions only weaken what a passed check means.
- **T9.** Each branch's store is fresh and isolated (`runner._fresh_state`, `init_subtask_store`); tools declared pure are.
- **A7.** Policies return ops and do not raise `RollbackRequested` (RB6).

**How the rollback model is tied to the Python** (`tests/lean/test_rollback_agreement.py`, with the Lean driver's `branch` command):
- 40 Hypothesis-drawn release-team audits on a PR (300 nightly): where rollbacks land; interventions aimed at the fork's agent, at another (RB1) or two at once; an applied or a vetoed op below the fork; a second rollback within or past the child's fork (RB3). For each child the model's `batchAt`, `desynced` and `finished` must match what the Python did, and, unless nested past its parent's fork, the child must keep its parent's fingerprints and applied ops below the fork and never desync (property 1 on real Orbit runs with scripted models).
- Fault injection: a tool declared pure that is not (desync), and a child halted before its fork (RB2).
- Not compared: the worlds themselves (only fingerprints and ledgers), live models (scripted only, so T8 failing on real models is not tested), and the shared state's own rules (B1, tested in `tests/contract/test_rollback.py`).

**Not covered.** Effectful tools (a fork past one is refused, R5); defences and model filters (refused); concurrent steps (refused); the policy's own memory beyond "arbitrary shared state"; the number of model calls within a boundary (RB5).

## Open decisions

- **D3.** Turns are in the view. Keep that.
- **Nested scopes.** If an L2 helper and an inner L1 ever share a controller, scopes must become per-policy, and property 5 needs a real statement.
