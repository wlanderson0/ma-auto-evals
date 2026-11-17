# Lean verification of rollback: plan

**Goal:** prove in Lean that a rollback is faithful. A child branch reproduces its parent exactly up to the fork, and differs after it only through the rollback's interventions and what happens live afterwards. Every L1 comparison between branches rests on this. The scope proofs (`lean/`, PR #41) deliberately left it out: there, each branch starts from `w0` and the run between boundaries (`Env.step`) is arbitrary per branch.

**Approach:** the same as the scope plan (`2026-11-09-lean-scope.md`). Extend the existing model rather than start a new one. Write small statements, freeze them, gate the proofs, and tie them to the Python with tests. The scope theorems must stay proved on the extended model.

## What the Python does (to map in step 1)

`branches.py` and `controller.py`:
- A child re-runs Orbit from scratch in a fresh store.
- At each revision before the fork it does three things:
  - checks the view's fingerprint against the parent's (`ReplayDesync` otherwise);
  - re-applies the parent's recorded ops, re-checked, without asking the policy;
  - serves the parent's recorded generations, and re-executes only pure tools.
- At the fork revision it applies the interventions, then goes live.
- Ids come from `BranchIds`: the same as the parent before the fork, the branch's own after.

## Properties

1. **Replay fidelity.** Assume Orbit's run between boundaries is a function of the world, the generations it is served and pure tool results. Then at every revision before the fork, a child reaches the same boundary, in the same world, as its parent, and applies the same ops with the same verdicts. At the fork, its world is the parent's world at that revision.
2. **Fail-safe.** Drop the assumption. A replayed boundary whose view differs from the parent's ends the branch before any op is applied there. So a nondeterministic Orbit produces a desync, never a silently different prefix.
3. **The rollback's own effect.** At the fork, every agent sees exactly what it saw at that revision of the parent. After it, the world changes only through the interventions and live ops, which the scope theorems already cover. This closes the "rollback is exempt" carve-out in `ScopeRespected`.
4. **No leakage.** A child's run depends only on `w0`, the parent's recorded prefix and the interventions, never on anything after the parent's fork point (R6).
5. **No-op rollback is the identity.** With no interventions, and the same policy and Orbit after the fork, the child's run is the plain run (R7).

## Model changes

- **The world between boundaries:** `Env.step` loses its branch argument. The world after a step becomes a function of the world before it and the generations served, which are live from an oracle or replayed from the recording. A replaced generation op counts as a served output.
- **Recording:** each `Step` records its boundary, its fingerprinted view, its batch and its served outputs, as in `branches.Step`.
- **Fingerprint:** a function of everything `fingerprint(view)` hashes. Hash collisions are an assumption (T7), not something modelled.
- **Desync:** `runBranch` ends a branch on a fingerprint mismatch, mirroring `controller.py:374-379`.
- **Ids:** model `BranchIds` well enough to state that ids allocated after a fork are unique to the branch. Check whether the model reproduces N4: nested rollbacks always desync. Classify it as a finding if they do.

## Steps

1. **Map:** `branches.py`, the replay paths in `controller.py` (`_view`, `_proceed`, `_decide`, `tool_call`), and `runner.py`. Add a file:line map, and the assumptions about Orbit's determinism and the store.
2. **Model:** the extension above, with mirror headers. The scope proofs must still pass.
3. **Statements:** properties 1–5 in the frozen spec, with witnesses: a run with a real fork; a desync; a no-op rollback; a nested rollback.
4. **Proofs:** under the same gate.
5. **Tie to the Python by checking real traces:**
   - Run the scripted release-team audits with rollbacks, through the existing contract fixtures and Hypothesis-generated policies.
   - Export each branch's recorded steps (boundary, fingerprint, ops and verdicts, outputs).
   - Have the Lean driver check them against properties 1, 2 and 4.
   - This covers the "episodes with replay" gap the scope work left open. R1–R7 stay as they are.
6. **Findings, CI, assurance:** as before. Extend `lean/ASSURANCE.md` rather than starting a second statement.

## Assumptions

- **T7.** No fingerprint collisions (SHA-256).
- **T8.** Between boundaries, Orbit is deterministic given the store, the generations it is served and pure tool results. Pure tools are pure as declared (`DEFAULT_TOOL_REPLAY`, `audit_task(tool_replay=...)`).
- **T9.** Each branch's store is fresh and isolated (`runner._fresh_state`, `init_subtask_store`).
- The scope plan's A1–A7 and T1–T6.

## Not covered

- Effectful tools: forks are refused, R5.
- Defences and model filters: forks are refused.
- Concurrent steps: refused.
- The policy's own memory across branches: it is meant to persist.
