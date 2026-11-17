# Lean verification of rollback: plan

**Goal:** prove in Lean that a rollback is faithful. A child branch reproduces its parent up to the fork, and differs after it only through the rollback's interventions and what happens live afterwards. Every L1 comparison between branches rests on this. The scope proofs (`lean/`, PR #41) left it out: there, each branch starts from `w0` and the run between boundaries (`Env.step`) is arbitrary per branch.

**Approach:** the same as the scope plan (`2026-11-09-lean-scope.md`): small statements, frozen, gated proofs, tied to the Python by tests. The new model goes in a **new spec file**; the frozen `Run.lean` is not edited. A simulation lemma carries the scope theorems over (step 4).

## What the Python does (to map in step 1)

`branches.py`, `controller.py`, `runner.py`:
- A child re-runs Orbit from scratch in a fresh store.
- At each revision before the fork it:
  - checks the view's fingerprint against the parent's (`ReplayDesync` otherwise, `controller.py:374-379`);
  - re-applies the parent's recorded ops, re-checked, without asking the policy or the approver, reusing the parent's `over_budget` verdict (`:513-518`) and not counting them again (B1, R3);
  - serves the parent's recorded generations, and re-executes only pure tools. A `ReplaceGeneration` output is not served: it is produced again by re-applying the op.
- A vetoed batch records no decisions (`:453`), so the child never sees it.
- At the fork it prepends the interventions to the woken policy's ops (`:432-433`), applied as one all-or-nothing batch, then goes live.
- Ids come from `BranchIds`: equal to the parent's below the fork, the branch's own after.
- State shared across branches: the ledger, the approver, the budget and branch count, the policy's memory, and the `Step` objects (`tuple(controller.steps[:k])` is a shallow copy).
- If no branch completes, the runner adopts the last one, even a desynced one (`runner.py:144`).

## Properties

1. **Replay fidelity (conditional on T8).** At every revision below the fork, the child reaches the same boundary as its parent, in a world equal on the T8 projection, and its *applied* entries equal the parent's. The non-circular part: those applied entries do not depend on the shared cross-branch state (ledger, approver, budget, policy). This catches removing the `over_budget` reuse, the approver skip, or the "replay does not count" rule. Rejected and vetoed entries are not claimed equal (vetoes are not re-recorded; a rejected rollback is rejected again for a different reason).
2. **Fail-safe (unconditional).** For every `step`, a replayed boundary whose fingerprinted view differs from the parent's ends the branch before any op is applied there. Claimed only on the fingerprinted projection, not hidden state (other agents' contexts, pure tools' store effects, the overlay, fewer model calls than recorded).
3. **Fork admissibility.** An accepted rollback, under fidelity, has every intervention pass `checkProblem` at the fork, together with the woken policy's ops. **Expected to fail on today's code:** `_rollback_problem` (`:483-504`) checks only the boundary. It does not check the target agent, the `tool_call_id` pin, `_SOLE`, that referenced messages exist at k, or the budget with the woken ops. So an accepted rollback can produce a child whose interventions are all rejected, which then runs as a no-op while `BranchNode.interventions` still lists them. Planned as a `bug_` theorem.
4. **The rollback's own effect (cross-branch).** For every agent outside the scope, its view in the child's world after the fork's ops equals its view in the parent's world at k. This replaces the carve-out in `ScopeRespected`, which cannot simply be dropped: a rollback entry has `pre = post` (`Run.lean:145`), so dropping it proves nothing. New decision **D4**: under an agent scope, is a rollback allowed when it rewinds and re-runs agents outside the scope? Unlike halt (D1), this changes what they say after k.
5. **No-op rollback is the identity, up to id renaming.** With no interventions and, as a hypothesis, the same policy and Orbit after the fork, the child's run equals the plain run after renaming ids (R7 compares after renumbering, `test_noop_rollback.py:60-71`).
6. **Ids.** The child's id stream equals the parent's below the fork, and ids it allocates after the fork are unique to it. **Expected to fail for nested rollbacks** (N4, already in `scope-map.md:78`): when the target is past the parent's own fork and an id minted between the two reaches a fingerprint, the child desyncs. Planned as a `bug_` theorem; the Python fix is to replay the ancestors' `diverge` points.
7. **Adoption.** Which branch the runner adopts and scores. Today it can be a desynced one; state what is adopted and decide whether that is acceptable (**D5**).

Dropped from the first draft: "no leakage" (R6). In the model it holds by construction, because the child is built only from `decisions.take t`, and no single trace can check it. The cross-branch sharing it should be about is covered by property 1's independence clause.

## Model (new spec file, `Scope/Spec/Branch.lean`)

- `step` keeps a nondeterminism argument (a seed or branch index). T8 is a **hypothesis** on it, not built into the type. Without that, property 1 restates T8 and property 2 cannot be stated.
- Explicit shared state: ledger, approver, budget, branch count, policy memory.
- `Step` records: boundary, fingerprinted view, batch, served outputs, `over_budget`.
- Fingerprint: a function of the projection `fingerprint(view)` hashes; collisions are T7.
- `runBranch` ends on a fingerprint mismatch; the fork batch is interventions ++ woken ops.
- `BranchIds` with `diverge`, enough for property 6.
- Fix the existing mirror first: the old model's replay re-applies vetoed batches (`Run.lean:216`), which the Python does not.

## Steps

1. **Map:** `branches.py`, the replay paths in `controller.py` (`_view`, `_proceed`, `_decide`, `_over_budget`, `_rollback_problem`, `tool_call`), `runner.py`. Write a file:line map, the list of shared state, and T8's excluded fields.
2. **Model:** `Branch.lean` as above, with mirror headers; fix the vetoed-batch mirror.
3. **Statements:** properties 1–7, frozen, with witnesses: a real fork; a desync; a no-op rollback; a nested rollback; an accepted rollback whose interventions are all rejected.
4. **Proofs:** under the same gate. Simulation lemma: every new-model run's entries are a sublist of an old-model run's entries under a derived `Env`. `Enforced` and `ScopeRespected` follow as corollaries, without re-proving them.
5. **Tie to the Python, model agreement (T4) on the replay and fork paths:**
   - Run the scripted release-team audits with rollbacks, through the contract fixtures and Hypothesis-generated policies.
   - Export, per boundary: the Lean `World`, `ctx`, batch, recorded `over_budget`, and the Python's verdicts and effects. Check them with Lean `decide`.
   - Inject faults (a perturbed store, a dropped model call) to exercise property 2; scripted runs never desync on their own.
   - This fills the "replay and fork paths: source reference only" gap in `ASSURANCE.md`. Exported SHA-256 fingerprints alone would only repeat R1/R2/R7.
6. **Findings, CI, assurance:** classify property 3 and 6 failures (fix or document), extend `lean/ASSURANCE.md`, record D4 and D5.

## Assumptions

- **T7.** No fingerprint collisions (SHA-256).
- **T8.** Between boundaries, Orbit is deterministic **on the Lean `World` projection**, given the generations served and pure tool results. Excluded explicitly: Inspect message ids, timestamps, mockllm tool-call ids, the `DeliveryOverlay`, `_generation_counts`, the `BranchIds` counter. To check in step 1: whether Inspect runs one message's tool calls in parallel.
- **T9.** Each branch's store is fresh and isolated (`runner._fresh_state`, `init_subtask_store`).
- The scope plan's A1–A7 and T1–T6.

## Not covered

- Effectful tools: forks are refused (R5). A fork at `TOOL_CALL` runs revision k live, guarded only by `fork_problem`'s scan; noted, not proved.
- Defences and model filters: forks are refused.
- Concurrent steps: refused.
- The policy's own memory across branches: shared on purpose; only modelled as shared state.
- Live models: trace checks use deterministic mockllm only, so T8 failing on real models is not tested.
