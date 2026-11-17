# Rollback: code map (rollback plan step 1)

Code at `b8447ed` (`sarah/lean-scope`), Orbit fork `Danhelo/orbit@dc4ea5e`, Inspect as locked in `uv.lock`. Paths are under `src/orbit_audit/` unless prefixed; Orbit and Inspect paths are under site-packages. Plan: `docs/superpowers/plans/2026-11-17-lean-rollback.md`.

## Shape of an audit

- **Branches form a chain.** `solve` (`runner.py:99-147`) runs one branch at a time. A branch queues at most one child, only when it ends with `RollbackRequested` (`runner.py:135-140`). So branch *i*'s parent is branch *i−1*, and every branch but the last is `rolled_back`.
- **A child** gets `Replay(parent, steps=tuple(controller.steps[:k]), fork_revision=k, interventions, reason)` (`runner.py:138-140`). `steps` holds the parent's `Step` objects by reference, but nothing in the child writes them: it reads `decisions`, `outputs`, `over_budget` and `fingerprint` (`controller.py:379, 392, 425, 515`).
- **Every branch** starts from a deep copy of the sample's initial state (`runner.py:104, 157, 200-210`), in a fresh Inspect store (`init_subtask_store`, `runner.py:158`), a copied context (`runner.py:180`), a fresh `BranchIds` (`runner.py:118`) and a fresh controller (`runner.py:119-123`).

## Per revision in a child (`r` = revision, `k` = fork revision)

| | `r < k` (replay) | `r = k` (fork) | `r > k` (live) |
|---|---|---|---|
| fingerprint checked against parent | yes, else `ReplayDesync` before any op (`controller.py:377-383`) | yes, same | no |
| ids | prefix 0 (`branches.py:127-136`) | `diverge(branch_index)` after the check (`controller.py:384-385`) | branch's own prefix |
| batch | parent's recorded `decisions` (`controller.py:424-425`) | interventions ++ woken policy's ops (`:432-433`) | policy (`:428`) |
| policy asked | no | yes; the LLM auditor always wakes on a new branch (`auditor/llm.py:111-115`) | yes |
| scope and `_problem` re-checked | yes (`:442, 528-549`) | yes | yes |
| `_SOLE` | yes (`:443-445`) | yes, over interventions ++ woken ops | yes |
| `over_budget` | parent's verdict reused (`:513-515`) | computed now, interventions ++ woken ops (`:517`) | computed |
| approver | skipped (`:557`) | consulted | consulted |
| rollback in batch | rejected: "not re-applied during replay" (`:467-469`) | checked (`:470-481`) | checked |
| applied ops count toward `max_interventions` | no (`:578`) | yes | yes |
| ledger record | `replayed=True` (`:591`); `notice` still called (`:594`) | `replayed=False` | `replayed=False` |
| generations | recorded `outputs` served through Orbit's replay seam (`:391-403`; Orbit `execution/control.py:290-307, 341-358`) | live (`:405-412`) | live |
| more model calls than recorded | `ReplayDesync` (`:396-398`) | n/a | n/a |
| fewer model calls than recorded | **not detected** | n/a | n/a |
| tools | pure: re-executed in the branch store; other: `ReplayDesync` (`:265-270`) | live, any tool (guarded only by `fork_problem` on the parent's steps, `branches.py:219-232`) | live |

**Generation ops on replay.** A `ReplaceGeneration` produces its output without calling `proceed`, so the parent records no output and the child produces it again by re-applying the op. An `EditGeneration` calls `proceed` with the edited input, and the child is served the parent's recorded output for it (`controller.py:218-233`).

**Vetoed batches.** An approver veto clears the step's `decisions` (`controller.py:448-453`), so the child sees an empty batch at that revision. Rejections for any other reason keep the batch, and the child rejects it again. Apply-time errors (`OpError`) depend only on the world.

## State shared across branches

Created once per sample in `solve` (`runner.py:100-103`), or passed in:

- **`policy`** (`auditor`): one instance. It keeps its memory, and `notice` is called during replay too (`controller.py:594`). The base `notice` does nothing (`policy.py:96-97`), and the LLM auditor drops replayed records (`auditor/llm.py:106-108`).
- **`BudgetLedger`**: provider calls, tokens, auditor calls, interventions, branches (`branches.py:257-298`). It is never refunded (B1).
- **`BranchTree`**: append-only. The policy reads it through `BranchQueries.branches` (`controller.py:148-150`).
- **`approver`**, which may be stateful (for example a model-backed realism check).
- **`scope`** and **`tool_replay`**: immutable.
- **Not part of the audit's state, but shared:**
  - the module-level model instances, including a mockllm script's cursor (live calls in any branch consume it);
  - Inspect's sample limits (time, messages, tokens);
  - the transcript;
  - anything module-level in a tool, as in the desync test's counter (`tests/contract/test_rollback.py:225-235`).

**Per branch**, so not shared:
- the `EvidenceLedger` (`runner.py` passes none, so `controller.py:171` makes one per controller);
- the overlay;
- `steps`, `generations`, `_generation_counts`, `turn` and `ending`;
- the store;
- `BranchIds`.

The plan's "ledger" in the shared-state list means the `BudgetLedger`, not the `EvidenceLedger`.

## What the fingerprint covers (`branches.py:178-196`)

**Covered:**
- boundary, agent, turn, generation index;
- the request input without Inspect message ids, which still include tool-call ids;
- tool names, tool name, `tool_call_id`, arguments, result;
- schedule revision and pending steps;
- canonical channel messages, **including Orbit message ids** (`CommunicationWorld.summary`, `delivery.py:91-96`).

**Not covered (hidden state):**
- the overlay (rewrites, drops, injections, `delivered`): it shows only through later inputs and tool results;
- the store effects of pure tools (`Suite`, `Ballot`, `Quota`, `RepoEnv` in `experiments/scenarios.py`), which feed scoring;
- each agent's conversation between its own generations;
- `CommunicationState.events`, channels and permitted edges;
- `tool_specs`;
- the revision and the branch.

## T8, made precise

*Assumption:* between boundaries, Orbit and Inspect are deterministic on the Lean `World` projection, given the generations served and pure tools' results.

**Checked:**
- Inspect runs one assistant message's tool calls sequentially unless a tool opts in with `parallel=True` (`inspect_ai/tool/_tool.py:184-187, 238`). Orbit's channel tools and the scenario tools don't opt in.
- Concurrent schedule steps are refused (`controller.py:618-627`).

**Excluded from the projection (they differ across branches by design):**
- Inspect message ids;
- `CommunicationEvent` ids and Inspect span, event and model-event ids (Orbit `communication/state.py:57-82`);
- live tool-call ids after the fork;
- `BranchIds` after the fork;
- the controller-local `_generation_counts`;
- the spend counters.

**Out of scope (left to T9):** module-level state in tools, and the scripted model cursor. The desync test relies on breaking exactly this.

## Lean model vs Python (`lean/Scope/Spec/Run.lean`)

- **M1.** The model records a vetoed batch in `decisions` (`Run.lean:216`, `decs ++ [ops]`), so its replay re-applies it; the Python clears it (`controller.py:453`). Fix in the new spec file.
- **M2.** The model takes `env.hooks b` and `env.step b` per branch: anything may differ, so replay fidelity cannot be stated. The new model makes the shared state explicit and keeps the nondeterminism as an argument.
- **M3.** `auditLoop` keeps a queue (`Run.lean:222-232`); the Python's is a chain. The queue is more general, so the scope theorems still hold, but the new model can use a chain.

## Findings so far (to reproduce and classify in step 6)

- **RB1. An accepted rollback can produce a child that rejects all its interventions** (property 3). `_rollback_problem` (`controller.py:483-504`) checks only that each intervention is allowed at the target's boundary kind. The child re-checks everything at the fork, so **the scope is never bypassed**. But the child rejects the whole batch (interventions included) when:
  - an intervention names another agent than the fork boundary's (`:544-545`);
  - it is pinned to another tool call (`:546-548`) or a stale revision (`:534-535`);
  - two producers meet at a `_SOLE` boundary, including the woken auditor's own generation op;
  - the woken auditor adds a rollback to the fork batch ("must be the only operation", `:470-472`);
  - a message op references a message that exists at the rollback's revision but not at `k`: the rollback's scope check uses the world at its own revision (`:484-485`);
  - interventions plus woken ops exceed `max_interventions`;
  - the approver vetoes at the fork.

  *Effect:* a branch is spent, and the child runs as a no-op rollback, while `BranchNode.interventions` (`branches.py:353`) lists ops that were never applied. The child's ledger does record the rejections.
  *Reachability:* the LLM auditor is always woken at the fork. Some of these exclusions may be intended (the approver vets content only once it is live); the rest need a decision or a fix.
- **RB2. A child that stops before its fork completes undetected** (property 2). The fingerprint check runs only when a boundary is reached. If a nondeterministic run ends before revision `k`, the child finishes `completed` with no interventions applied, and nothing compares its final revision with `k` (`runner.py:167-175`). Being the last branch, it is adopted and scored. This needs T8 to fail. *Fix:* end with `ReplayDesync` when `replay` is set and `controller.revision < fork_revision`.
- **RB3 (N4). A nested rollback above the parent's fork desyncs.** The child's ids keep prefix 0 up to its own fork, while the parent switched to its own prefix at its fork. The first boundary after a message minted between the two forks therefore sees different message ids (`delivery.py:93`), and the branch fails safe. Below the parent's fork, ids agree. *Fix:* replay the ancestors' `diverge` points.
- **RB4. The adopted branch is always the last one.** Because branches form a chain, `completed or outcome` (`runner.py:144`) is the last branch even when it ended `replay_desync` or `budget_exhausted`, and Orbit's scorers score it. Decision **D5**.
- **RB5. Fewer model calls than recorded go undetected** (`controller.py:391-403`). Under T8 they cannot happen; without T8 the fingerprint at the next boundary usually catches the effect. This limits property 2's wording.

## Existing tests this maps to

- **`tests/contract/test_rollback.py`:**
  - R1/R2: `:111`;
  - R3: `:123, 133`;
  - R4: `:76`;
  - R5: `:187`;
  - R6: `:138`;
  - desync by fault injection: `:238`;
  - B1: `:250-300`;
  - replayed ops keep their verdict: `:300`.
- **`tests/contract/test_noop_rollback.py:112`:** R7, after renumbering ids (`:60-71`).
- **Not tested:**
  - a nested rollback above the parent's fork (RB3);
  - a fork batch rejected at the child (RB1);
  - a child stopping before its fork (RB2).
