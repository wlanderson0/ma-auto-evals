# Rollback: code map (rollback plan step 1)

Code at `b8447ed` (`sarah/lean-scope`), Orbit fork `Danhelo/orbit@dc4ea5e`, Inspect 0.3.263 (as locked in `uv.lock`). Paths are under `src/orbit_audit/` unless prefixed. Orbit and Inspect paths are under site-packages and were read in the installed versions. Plan: `docs/superpowers/plans/2026-11-17-lean-rollback.md`. This map was reviewed adversarially and revised.

## Shape of an audit

- **Branches form a chain.** `solve` (`runner.py:99-147`) runs one branch at a time.
  - A branch queues at most one child, and only when its ending is a `RollbackRequested` (`runner.py:135-140`).
  - So branch *i*'s parent is branch *i−1*, and every branch but the last ended `rolled_back`.
  - An `ERROR` branch is adopted, then its error is re-raised (`runner.py:141-143`).
- **The adopted branch is always the last** (`runner.py:144`, `completed or outcome`): see RB4.
- **A child** gets `Replay(parent, steps=tuple(controller.steps[:k]), fork_revision=k, interventions, reason)` (`runner.py:138-140`).
  - `steps` holds the parent's `Step` objects by reference.
  - The child's own code only reads them, at `controller.py:379, 392, 425, 515`.
  - At `:399` it hands a recorded `ModelOutput` object to Orbit's replay seam. Orbit copies what it returns (Orbit `execution/control.py:307`), but whether the seam mutates its input has not been checked.
- **Every branch** starts with:
  - a deep copy of the sample's initial state (`runner.py:104, 157, 200-210`; `model` and `target` are shared by reference);
  - a fresh Inspect store (`init_subtask_store`, `runner.py:158`), which holds Orbit's communication state and message sequence counter (Orbit `communication/runtime.py:306-308`);
  - a copied context (`runner.py:180`);
  - a fresh `BranchIds` (`runner.py:118`);
  - a fresh controller (`runner.py:119-123`), which calls `policy.bind` and `policy.start` on the shared policy (`controller.py:169, 196`).

## Per revision in a child (`r` = revision, `k` = fork revision)

| | `r < k` (replay) | `r = k` (fork) | `r > k` (live) |
|---|---|---|---|
| fingerprint checked against parent | yes, else `ReplayDesync` before any op (`controller.py:377-383`) | yes, same | no |
| ids | prefix 0 (`branches.py:127-136`) | `diverge(branch_index)` after the check (`controller.py:384-385`) | branch's own prefix |
| batch | parent's recorded `decisions` (`controller.py:424-425`) | interventions ++ the woken policy's ops (`:432-433`); if the policy raises, the branch ends before the interventions are added (`:428-431`) | policy (`:428`) |
| policy asked | no | yes, but the LLM auditor may return nothing (`summary` set, `max_wakes` spent: `auditor/llm.py:113-116`) or raise (its budget is spent: `:118-119`) | yes |
| scope and `_problem` re-checked | yes (`:442, 528-549`), except in a batch with a rollback, which `_rollback` rejects (`:438-441, 467-469`) | yes, at the fork's world and pending steps | yes |
| `_SOLE` | yes (`:443-445`) | yes, over interventions ++ woken ops | yes |
| `over_budget` | parent's verdict reused (`:513-515`); the batch is the parent's, so it is the same batch | computed now, interventions ++ woken ops (`:517`) | computed |
| approver | skipped (`:557`) | consulted | consulted |
| applied ops count toward `max_interventions` | no (`:578`) | yes | yes |
| ledger record | `replayed=True` (`:591`); `notice` still called (`:594`) | `replayed=False` | `replayed=False` |
| generations | recorded `outputs` served through Orbit's replay seam (`:391-403`; Orbit `execution/control.py:290-307, 341-358`) | live (`:405-412`) | live |
| more model calls than recorded | `ReplayDesync` (`:396-398`) | n/a | n/a |
| fewer model calls than recorded | **not detected** (RB5) | n/a | n/a |
| tools | pure: re-executed in the branch store; other: `ReplayDesync` (`:265-270`) | live, any tool (guarded only by `fork_problem` on the parent's steps, `branches.py:219-232`) | live |

**Generation ops on replay** (`controller.py:214-233`, `ops.py:119-185`). The child re-applies the recorded op, and it is served the parent's recorded outputs in order:
- `ReplaceGeneration` produces its output without calling `proceed`, so nothing is recorded and nothing is served.
- `EditGeneration` calls `proceed()` on the original input and edits the output (`ops.py:153-167`). The child is served the raw recorded output and applies the edit again.
- `ForceToolCall` validates and mints `call_` ids from the id stream before `proceed` (`ops.py:178`), then replaces the output's tool calls.
- **`OpError`** (`controller.py:221-226`):
  - with a provider output, that output is delivered unmodified;
  - without one, `_traced_proceed` makes another call, and in replay that call is served the next recorded output.

**Vetoed batches.** An approver veto clears the step's `decisions` (`controller.py:448-453`), so the child sees an empty batch at that revision. Rejections for any other reason keep the batch, and the child rejects it again. Apply-time errors (`OpError`) reject the op alone and depend only on the branch state.

## State shared across branches

Created once per sample in `solve` (`runner.py:100-103`), or once per solver (`runner.py:93-97`):

- **`policy`** (`auditor`): one instance, which keeps its memory.
  - `bind` and `start` are called once per branch (`controller.py:169, 196`).
  - `notice` is called during replay too (`controller.py:594`). The base `notice` does nothing (`policy.py:96-97`); the LLM auditor drops replayed records (`auditor/llm.py:106-108`).
- **`BudgetLedger`**: provider calls, tokens, auditor calls, interventions, branches (`branches.py:257-304`). Never refunded (B1).
- **`BranchTree`**: append-only. The policy reads it through `BranchQueries.branches` (`controller.py:148-150`).
- **`MeasurementRecorder`** (`runner.py:103`). It reads the shared transcript, and it is evidence only.
- **`approver`**, which may be stateful (e.g. a model-backed realism check).
- **`scope`** and **`tool_replay`**: immutable.
- **The `mas_orchestrator` solver instance** (`runner.py:93-96`): built once, shared by every branch. Whether it holds state between runs has not been checked.
- **Outside the audit's state, but shared:**
  - module-level model instances, including a mockllm script's cursor (live calls in any branch consume it);
  - Inspect's sample limits (time, messages, tokens), which are cumulative across branches;
  - the transcript;
  - module-level state in tools.

**Per branch:**
- the `EvidenceLedger`: `runner.py` passes none, so `controller.py:171` makes one per controller;
- the overlay;
- `steps`, `generations`, `_generation_counts`, `turn` and `ending`;
- the store;
- `BranchIds`.

The plan's "ledger" in the shared-state list means the `BudgetLedger`.

## What the fingerprint covers (`branches.py:178-196`)

**Covered:**
- boundary, agent, turn;
- the generation index (`_generation_counts`, `controller.py:204`);
- the request input, minus Inspect message ids (tool-call ids stay in);
- the tool names, the tool name, `tool_call_id` and the arguments;
- the tool result, through `canonical`, which drops pydantic `id`s and hashes non-JSON values via `str()`. A `str()` holding a memory address could cause a spurious desync, which is the fail-safe direction;
- the schedule revision and pending steps;
- canonical channel messages, **including Orbit message ids and sequence numbers** (`CommunicationWorld.summary`, `delivery.py:91-96`).

**Not covered (hidden state):**
- the overlay (rewrites, drops, injections, `delivered`), which shows only through later inputs and tool results;
- injected messages, which are not in `summary()`;
- the store effects of pure tools (`Suite`, `Ballot`, `Quota`, `RepoEnv` in `experiments/scenarios.py`), which feed scoring;
- each agent's conversation between its own generations;
- the schedule's other state (e.g. halted);
- `CommunicationState.events` and receipts, channels and permitted edges;
- `tool_specs`;
- the revision and the branch;
- ids that are minted but not yet seen: a `ForceToolCall`'s call ids until they appear in an input, and `StageInput` and assistant message ids (excluded by `exclude={"id"}`).

## T8, made precise

**Branch state** `S`:
- the store: Orbit communication and delivery state, the message sequence counter, pure tools' store models;
- each agent's conversation;
- the schedule runtime;
- the overlay;
- the `BranchIds` state `(prefix, count)`;
- `_generation_counts` and `turn`.

**Equivalence** `≈`: equal except for these identifiers, which are random or deliberately branch-specific:
- Inspect message ids, timestamps, and span, event and model-event ids;
- `CommunicationEvent` ids (Orbit `communication/state.py:57-82`).

**T8.** Two runs from states `S ≈ S'`, served the same generations and the same pure tool results, reach the same next boundary, in states that are again `≈`. The Lean `World`, the boundary view and the fingerprint are functions of `S`, and they respect `≈`. Equal `BranchIds` state is part of the hypothesis: RB3 is where it fails.

**Checked in the installed versions:**
- Inspect 0.3.263 runs one assistant message's tool calls in order. Consecutive calls run concurrently only if their tools declare `parallel=True`. The default is `False`, and unknown tools are serial (`inspect_ai/tool/_tool.py:184-187, 238`; `inspect_ai/model/_call_tools.py:345-367`). Orbit's channel tools and the scenario tools don't declare it.
- Explicit plans with concurrent steps are refused (`controller.py:193-194, 618-627`). Whether there is concurrency without an explicit plan is taken from the scope map's A5.

**Not T8's job:**
- **Tools declared pure that are not** (module-level state, as in the desync test's counter, `tests/contract/test_rollback.py:225-235`) are a false `PURE` declaration (`branches.py:97-99`). They are covered by the assumption that declarations are true.
- **The scripted model's cursor** is used only by live calls.

## Lean model vs Python (`lean/Scope/Spec/Run.lean`)

- **M1. Vetoed batches.**
  - The model records a vetoed batch in `decisions` (`Run.lean:216`, `decs ++ [ops]`), so its replay re-applies it; the Python clears it (`controller.py:453`).
  - This does not make the frozen scope proofs unsound: the old model is more general, and `env.hooks b` can veto again in any branch.
  - The new spec file mirrors the Python.
- **M2. Nothing shared across branches.** The model takes `env.hooks b` and `env.step b` per branch, so anything may differ between branches, and replay fidelity cannot be stated. The new model makes the shared state explicit and keeps the nondeterminism as an argument.
- **M3. Queue vs chain.** `auditLoop` keeps a queue (`Run.lean:222-230`); the Python's branches form a chain. The queue is more general, so the scope theorems still hold, but the new model can use a chain.
- **M4. Policies cannot raise.** `Policy` returns a list of ops (`Run.lean:177`), so a policy that raises is outside the model (see RB6).
- **M5. Stale line numbers.** `Run.lean`'s mirror headers cite controller lines from before the G1–G3 fixes. The new file cites current lines, and the old headers are noted in step 6.

## Findings so far (to reproduce and classify in step 6)

### RB1. An accepted rollback can produce a child that rejects its interventions

This is property 3.

**What `_rollback_problem` checks** (`controller.py:483-504`), and the gap:
- It scope-checks the rollback, recursing into each intervention (`scope.py:66-74`), and checks each intervention's boundary kind and the intervention count.
- But it judges these against the world and pending steps **at the rollback's revision**, not at `k`.
- It skips `_problem`'s other checks: agent, tool-call pin, stale revision, unknown agents.
- The child re-checks every op at `k` with the fork's world and pending steps, so **no op is applied outside the scope through a fork** (subject to RB6 and D4).

**The child rejects the whole batch, interventions included, when:**
- an intervention names an agent other than the fork boundary's (`:544-545`);
- it is pinned to another tool call (`:546-548`), names a stale revision (`:534-535`), or names unknown agents (`:536-539`);
- the scope rejects it at `k` although it passed at the rollback's revision, because the pending steps differ. Example: an `amend_pending` intervention under `agents={"reviewer"}` (pattern of `ForkAndAmend`, `tests/contract/test_schedule_amendments.py:40-58`);
- two producers meet at a `_SOLE` boundary (`:443-445`): two among the interventions, or one plus the woken policy's;
- the woken policy adds a rollback (`:470-472`);
- interventions plus woken ops exceed `max_interventions`;
- the approver vetoes at the fork.

**The child rejects a single intervention, at apply time,** when it is a message op on a message that does not exist at `k` or was already delivered by `k` (`ops.py:292-304`), or an `amend_pending` whose schedule revision is stale at `k` (`ops.py:431-438`).

**The child ends at `k`, with nothing applied,** when the woken policy raises:
- `BudgetExhausted` (`auditor/llm.py:118-119`, `policy.py:86-87`, `auditor/seat.py:53-55`);
- or an Inspect limit, classified as `budget_exhausted` (`runner.py:170`).

`_rollback_problem` checks neither budget. Being the last branch, the child is then adopted (RB4).

**Effect:** a branch is spent, and the child runs as a no-op rollback, or not at all. Meanwhile `BranchNode.interventions` (`branches.py:353`) lists ops that were never applied; the child's ledger records the rejections.

**Reachability.** The shipped LLM auditor's `rollback` tool takes only `to_revision` and `reason` (`auditor/llm.py:397-406`), so its rollbacks carry no interventions. That leaves:
- Python or scripted policies;
- the LLM auditor's budget case.

**Concrete cases** (both from `tests/contract/test_rollback.py`):
- `RollbackBeforeVerdict(interventions=(ReplaceGeneration(agent="coder", content="x"),))` is accepted. At `k`, the pm's generation rejects it ("targets ['coder'] but this boundary is 'pm''s"), and the child completes unchanged.
- `interventions=(SHIP_IT, ReplaceGeneration(agent="pm", content="y"))`: `_SOLE` rejects both.

**Some exclusions may be intended,** e.g. the approver vets content only once it is live. The rest need a decision or a fix.

### RB2. A child that ends before its fork can be adopted as if it had forked

This is property 2.

**The mechanism.**
- The fingerprint check runs only when a boundary is reached.
- If the child's run ends before revision `k`, nothing compares its final revision with `k` (`runner.py:167-175`), and no intervention is applied.
- Its ids never diverge, so any live ids are prefix 0 and collide with branch 0's.
- Being the last branch, it is adopted and scored.

**Ways to get there:**
- *T8 fails:* the run finishes early and the child ends `completed`.
- *Under T8:* Inspect's sample limits are cumulative across branches (the scenarios set `token_limit=2_000_000, time_limit=900`, `experiments/scenarios.py:373, 687`), so the child ends `budget_exhausted` (`runner.py:170`).
- *Fault-injection repro:* through `on_controller`, halt the child's runtime before `k`.

**Cannot happen:**
- a policy halt during replay (the policy is not asked);
- a provider-budget stop on served calls (`controller.py:391-403`);
- a replayed `halt` or `amend_pending`, which is the parent's own and identical under T8.

**Fix:** record "fork not reached" on any ending with `replay` set and `controller.revision < fork_revision` (`ReplayDesync` when the ending is otherwise `completed`). State property 2 so that it covers run length.

### RB3 (N4). A nested rollback above the parent's fork uses the wrong ids

**The mechanism.**
- The child keeps prefix 0 up to its own fork; the parent switched to its own prefix at its fork.
- **It fails safe** if a differing id reaches a fingerprint at or before the child's fork, e.g. any channel message posted between the two forks (`delivery.py:93`).
- **Otherwise the divergence is silent:** injected messages, ids not yet seen, and the id counter.
- Below the parent's fork, ids agree.

**Fix:** replay the ancestors' `diverge` points.

### RB4. The adopted branch is always the last one

Because branches form a chain, `completed or outcome` (`runner.py:144`) is the last branch, even when it ended `replay_desync` or `budget_exhausted`, or never reached its fork (RB2). Orbit's scorers score it. **D5, decided:** such a branch is not scored; every score is incomplete and names how the branch ended (`experiments/scorers.py`, `scored_view`).

### RB5. Fewer model calls than recorded go undetected

See `controller.py:391-403`. Under T8 this cannot happen. Without T8, the fingerprint at the next boundary usually catches the effect. This limits property 2's wording.

### RB6. A policy that raises skips every rollback check

**The mechanism.**
- `_decide` re-raises any `BranchEnded` from `policy.decide` (`controller.py:429-431`).
- `policy.start` is uncaught (`:196`).
- The runner queues a child for any `RollbackRequested` it finds (`runner.py:135-140, 183-197`).

**So a policy that raises `RollbackRequested(Rollback(...))` skips:**
- the scope check;
- `max_branches` (the loop has no other bound);
- R5's `fork_problem`;
- the intervention budget;
- the roll-forward check. A `to_revision` past the parent's steps fails through `Replay.step` (`branches.py:170-175`).

**Status:** this sits outside the scope map's A7 ("policies only return ops"). Latent: no policy in the repo raises it.

**Fix:** queue only rollbacks that `_rollback` minted, or route a raised one through `_rollback`. Until then, A7 is a precondition of every rollback theorem.

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
- **Not tested:** RB1, RB2, RB3 above the parent's fork, RB6.
