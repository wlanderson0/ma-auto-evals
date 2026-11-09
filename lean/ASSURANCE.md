# Assurance statement: audit scope

**Pinned to:** branch `sarah/scope-g2-fix`: the Python of `src/orbit_audit/` with the G2 fix, and the specification and proofs updated with it, in the same commit; Lean `v4.35.0`, core and `Std` only; Orbit fork `Danhelo/orbit@dc4ea5e`. Specification: `lean/Scope/Spec/*.lean`, listed in `lean/Scope/Spec/SHA256SUMS` (sha256 `5902c390853d28f880424001d34361ead908ad19ee5ad5068953f6bcb6753fed`). Gate: `lean/check.sh` and the files it runs, listed in `lean/GATE.SHA256SUMS` (sha256 `853d462059725e87c4108496bd64b8374221d03bdaa7f23036b931d2f0fda323`).

**Status:** the theorem statements have had three adversarial reviews by AI agents. They have **not yet had the human review** the plan requires before the claim is relied on.

## The claim

> `Scope.scopeRespected`: in every run of the model of an audit under an `AuditScope s`, every op the controller applies was checked against `s` in the world it changed. And every applied op other than `halt` and `rollback` leaves each agent outside `s`'s agent limit, and outside what the open gap G1 can reach, seeing exactly what it saw before. This is proved about the Lean model. That the model agrees with the Python is tested, not proved.

"Seeing" means the specification `view` (`View.lean`):
- the agent's own generations, staged context and tool results;
- the channel messages it is shown, after the delivery overlay;
- its pending turns and their positions;
- whether the run is halted;
- the words the overlay attributes to it (the current rewrite of each of its messages, and injections sent as it).

## Limits of the claim, up front

- **The open gap G1.** For `amend_pending` the claim says only that the damage is bounded: it reaches at most the agents in the pending steps it replaces. The intended frame (`Frame`) is false while G1 is open (`bug_G1_amend_pending`). G2 is fixed (`G2Fixed`).
- **`halt` and `rollback`** are exempt by name. `rollback`'s own effect, and whether a fork reproduces its parent's world, are not modelled.
- **Only the immediate effect of each op.** The frame covers what an op changes at once. Words an op causes to be posted later are not covered: a seat's generation that posts to a channel, or a forged `channel_read` result.
- **Unique message ids.** The frame assumes them (`World.WF`). The model does not derive this for the worlds a run reaches, because the steps between boundaries are arbitrary.
- **Property 5 holds by construction in the model.** At the Python level it rests on T1 and T2.

## What is proved

| # | English | Lean statement | Proof | Python | Tied to the Python by | Witness |
|---|---|---|---|---|---|---|
| 1 | A clean scope check means the op name and every affected agent are allowed, and so is each rollback intervention | `ScopeSound` | `scopeSound` | `scope.py:52-71` | `test_problem_and_affected`, saved examples | `AgentsMutantWitness` |
| 2 | An op that passes the controller's other checks and applies leaves every agent outside `affected` and `gapLeak` unchanged | `FrameUpToGaps` | `frameUpToGaps` | `ops.py`, `controller.py:198-298`, `delivery.py:37-58` | `test_message_op_effects`, `test_turn_op_effects`, `test_frame_on_python_affected` | `FrameWitness`, `SeatWitness`, `HaltWitness` |
| 2′ | Without the G1 allowance, 2 is false | `¬ Frame` | `bug_G1_amend_pending` | G1 | `tests/contract/test_scope_gaps.py` (strict expected failure on a mock run) | — |
| 2″ | G2 fixed: an op naming no agent is refused, and one accepted at an agent's boundary names exactly that agent | `G2Fixed` | `g2Fixed` | `ops.py:95-96, 189-190, 221-222`, `controller.py:536-541` | `test_g2_*` in `tests/contract/test_scope_gaps.py` and `tests/lean/test_differential.py` | — |
| 3 | Every op an audit applies, live, replayed or at a fork, passed the scope check in the world it changed | `Enforced` | `enforced` | `controller.py:412-541`, `runner.py:99-147` | live batches: `test_controller_problem`, `test_decide`; replay and fork paths (`controller.py:419-429`): source reference only; AST inventory (T1) | `RunWitness`, `ReplayMutantWitness` |
| 4a | Widening a scope never rejects more | `Monotone` | `monotone` | `scope.py:49-71` | — | `MonotoneWitness` |
| 4b | The meet of two scopes allows exactly what both allow (no Python counterpart yet) | `Meet` | `meet` | — | — | `MeetWitness` |
| 4c | At a boundary that does not roll back, the world changes only through the ops applied there | `OnlyApplied` | `onlyApplied` | `controller.py:412-458` | `test_decide` | `OnlyAppliedWitness` |
| 5 | Every applied op was checked against the audit's own scope | `Confined` | `confined` | `runner.py:119-123` | T1, T2 | — |
| H | The headline: 1–3 and 5 | `ScopeRespected` | `scopeRespected` | all of the above | all of the above | `RunWitness` |

- **Axioms.** Every proof depends only on `propext`, `Classical.choice` and `Quot.sound` (some witnesses on `propext` alone). `leanchecker` re-checks the 11 `Scope.*` modules.
- **Size.** 69 proved theorems (lemmas included) and 10 frozen witness statements.
- **Representation witnesses** (`RepresentationWitness`): `none` vs `some []` for scope limits and for message recipients, and nested rollbacks.
- **The gate** (`check.sh`) fails if:
  - the specification or the gate changes without a new checksum;
  - a proof file imports anything but `Scope.*`;
  - a proof file uses one of a list of constructs that can add axioms or run code;
  - a statement or witness has no proof of exactly its type;
  - an axiom is outside the allow-list;
  - there is any warning.

### Traceability

| Statement | Specification definitions it rests on | Axioms | Statement reviewer |
|---|---|---|---|
| `ScopeSound` | `AuditScope.problem`, `Op.affected`, `Op.interventions` | propext, Classical.choice, Quot.sound | AI only |
| `FrameUpToGaps` | `applyRaw`, `checkProblem`, `view`, `gapLeak`, `carvedOut`, `World.WF` | same | AI only |
| `Enforced` | `runAudit`, `decide`, `rollbackProblem`, `AuditScope.problem` | same | AI only |
| `Monotone`, `Meet` | `AuditScope.Le`, `AuditScope.meet`, `AuditScope.problem` | same | AI only |
| `OnlyApplied` | `decide`, `Chain` | same | AI only |
| `Confined` | `runAudit` | same | AI only |
| `ScopeRespected` | all of the above | same | AI only |

## How the model is tied to the Python

- **Source references.** Every mirror definition cites the Python lines it follows. `tests/structural/test_lean_mirrors.py` checks that each of the 69 references is in range, and that the 28 that name a function or class still name a `def`/`class` there. The citations in this file are not checked.
- **Differential tests** (`tests/lean/`) run the Lean model as a subprocess against the real code:
  - 6 property tests, at 1000 examples each on a PR and 5000 nightly;
  - 22 saved examples covering the gaps, the representation cases and the batch and rollback rules;
  - codec tests (`null` vs `[]` vs a missing field, duplicate keys, any Unicode);
  - a check that every registered op has a Lean counterpart.

  The tests take about 45 seconds locally.
- **Mutation adequacy.** 7 mutants of the Python are each caught:
  - the scope check ignoring agents;
  - `affected` ignoring implicit recipients;
  - no targeted check;
  - delivery ignoring per-recipient drops;
  - no sole-producer rule;
  - rollback skipping the scope check;
  - `[]` read as `None`.

  The plan's G1, D1 and replay-skips-the-check mutants are not among them (G1 is a strict expected failure in `tests/contract/test_scope_gaps.py`; the replay mutant is a Lean witness). An earlier review ran 20 mutants by hand. Two survived 300 examples but were killed at 3000: an injection ignoring routes, and one ignoring readers. One was never killed: delivery not recording `delivered`.
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
  - whole episodes, and the replay and fork paths;
  - a corpus replayed from real runs.
- **Not modelled:**
  - the shared message id and sequence counters, and `schedule.revision`;
  - the world between boundaries;
  - log completeness, and faithful logs or transcripts.
- **Nested scopes.** The Python gives one controller one scope.
- **Outside the model entirely:** LLM behaviour beyond "an arbitrary function", Orbit and Inspect internals beyond A1–A7, concurrency, performance and denial of service.

## Findings

See `lean/scope-map.md`, "Findings (step 6)":
- **G1** (latent, reproduced): `amend_pending` under-reports `affected`.
- **G2** (fixed on `sarah/scope-g2-fix`): an empty `agent` bypassed the agent limit.
- **G3** (latent): an uncaught schedule error.

G1 and G3 each have a suggested fix, for a separate PR.

## Open decisions

- **D1.** Keep `halt` exempt, or count it as affecting every agent and carve it out of the seat brief.
- **D2.** What a delivery is. Option (a), only overlay changes, is the current choice and matches the seat test. Option (b), anything another agent reads, would make the tool-result forgery a finding.
- **D3.** Turns are in the view. Keep that.
- **Fixing G1.** Fixing it in the Python changes the model: `bug_G1_amend_pending` then becomes a `G1Fixed` statement, `gapLeak` becomes empty and `Frame` itself can be proved.
- **Nested scopes.** If an L2 helper and an inner L1 ever share a controller, scopes must become per-policy, and property 5 needs a real statement.
