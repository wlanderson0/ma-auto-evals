# Scope enforcement: code map (plan step 1)

Code at `b7b1eeb` (`sarah/l2-review-and-stops`), Orbit fork `Danhelo/orbit@dc4ea5e`. All paths are under `src/orbit_audit/` unless prefixed. Orbit paths are under `orbit/` in site-packages. Reviewed adversarially once; G2 reproduced.

## What "affect" means

`scope.py:32-34`: the agents an op "may affect" are those whose generations it writes, whose context it stages, or whose deliveries it changes. These are direct effects only: what an agent does afterwards in response is the later information-flow theorem. Turns, canonical sends and tool results are not covered by this wording: see D1–D3.

## Ops

`affected` = what `AuditScope.problem` checks (`scope.py:61-62`). "Directly affected" = who `apply` actually changes.

| Op | Boundary | `affected(world)` | `apply` changes | Directly affected | OK? |
|---|---|---|---|---|---|
| `replace_generation` | GENERATION | `(agent,) if agent else ()` (`ops.py:75-82`) | the boundary agent's generation (`ops.py:119-128`, `controller.py:215-230`) | boundary agent; on a channel tool call, also the post's recipients | ✗ G2, ? D2 |
| `edit_generation` | GENERATION | as above | as above (`ops.py:150-164`) | as above | ✗ G2, ? D2 |
| `force_tool_call` | GENERATION | as above | as above (`ops.py:174-181`) | as above | ✗ G2, ? D2 |
| `stage_input` | BEFORE_ACTIVATION | as above | appends to the boundary agent's conversation (`ops.py:209-214`) | boundary agent | ✗ G2 |
| `block_tool_call` | TOOL_CALL | as above | boundary agent gets a tool error; tool not run (`controller.py:254-256`) | boundary agent; on `channel_post`, the would-be recipients | ✗ G2, ? D2 |
| `simulate_tool_result` | TOOL_CALL | as above | boundary agent gets the result; tool not run (`controller.py:257-259`) | as above; on `channel_read`, can forge any sender's messages | ✗ G2, ? D2 |
| `override_tool_result` | TOOL_RESULT | as above | result the boundary agent sees (`controller.py:270-276`) | boundary agent; on `channel_read`, the same as a rewrite, which also counts the sender | ✗ G2, ? D2 |
| `rewrite_message` | any | recipients (explicit, or all of `m.recipients` minus sender) ∪ sender (`ops.py:270-281, 311-315`) | `overlay.rewrites` (`ops.py:317-322`) | recipients; sender by attribution | ✓ |
| `drop_message` | any | recipients, as above, without sender | `overlay.drops` (`ops.py:330-333`) | recipients | ✓ |
| `inject_message` | any | sender ∪ recipients (`ops.py:351-352`) | `overlay.injections` (`ops.py:354-381`) | recipients; sender by attribution | ✓ |
| `halt` | BETWEEN_TURNS | `()` | stops the run (`ops.py:397-398`) | every agent's remaining turns | ? D1 |
| `amend_pending` | BETWEEN_TURNS | agents in the **new** steps (`ops.py:410-411`) | replaces **all** pending steps of the current cycle (`ops.py:413-422`, Orbit `execution/plans.py:255-268`) | new-step agents ∪ old-pending agents | ✗ G1 |
| `rollback` | any | interventions' `agents()` (`ops.py:448-449`), plus recursion on each intervention with `world` (`scope.py:66-70`) | ends the branch; child replays to `to_revision` and applies interventions (`runner.py:135-140`) | interventions' agents | ✓ N3 |

## Enforcement path

- **One gate.** Every batch, live, replayed (`controller.py:420-421`) or at a fork with the rollback's interventions (`controller.py:428-429`), goes through `_decide` (`controller.py:412-458`). A non-rollback op is checked by `_problem` → `scope.problem` (`controller.py:526-527`); a rollback by `_rollback_problem` → `scope.problem` (`controller.py:481-482`). Scope is checked before any other check.
- **All-or-nothing at check time only.** Any problem found by the checks rejects the whole batch (`controller.py:450-454`); budget and approver can only add rejections (`442-449`). Apply-time errors reject one op alone, after the batch's message ops were already applied (`455-457` before `218-223`, `239-241`, `291-294`, `519-521`). A raising `policy.notice` (`controller.py:577`) can abort a batch midway.
- **Apply sites,** all fed only by `_decide`'s return value: `generate` (`controller.py:211-230`), `before_activation` (`236-242`), `tool_call` (`251-260`, `270-276`), `after_turn` (`288-295`), message ops (`455-457` → `_apply_message` `516-522`), rollback (`474-476`). Apply sites act on the **boundary's** agent (`request`/`activation`/`call`), not on `op.agent`.
- **Delivery** reads the overlay (`controller.py:297-298`, `delivery.py:37-53`). Message ops write rewrites/drops/injections; reads write `overlay.delivered` (`delivery.py:52`), which `_target` checks.
- **No bypass, by convention only.** At this commit nothing in `experiments/` or `src/` writes the overlay, schedule or `runtime.halt` outside ops, and nothing overrides `LLMAuditor._dispatch` (`auditor/llm.py:162`). But a Python policy can reach the live controller through `BranchQueries._controller` (`controller.py:126-127`), `on_controller` gets it too (`runner.py:125-126`), and `_decide` accepts any `ControlOp` subclass, registered or not (`controller.py:431`).
- **Inventory.** `lean/scope-inventory.json` lists the 24 sites (in `src/orbit_audit/` and `experiments/`) that apply an op, change the schedule or run end, or write delivery, schedule, scope or message-store state, each with its role. `tests/structural/test_scope_inventory.py` fails on any new or vanished site. Outside the controller and the ops' own `apply`, there are only: construction (`AuditController.__init__`), read bookkeeping (`DeliveryOverlay.deliver`), `AuditPolicy.bind` taking the scope, and `runner._adopt` after the run. It cannot see reads of the live controller (`BranchQueries._controller`, `on_controller`) or `object.__setattr__`-style writes through them.
- **Same scope on every branch:** `runner.py:119-123`. **`scope=None` means no check** (`controller.py:481, 526`).

## Assumptions (not modelled; preconditions of every theorem)

- **A1.** Channel messages reach a model only through `read_messages` → `controlled_delivery` (`communication/runtime.py:281-303`): `m` reaches `r` only if `r` is the sender, or `r ∈ m.recipients` and the route is permitted. `m.recipients` never contains the sender (`runtime.py:247`; `InjectMessage` rejects it, `ops.py:361`). Holds only when:
  - observation mode is `none` (modes `summary`, `peer_messages`, `custom` copy peers' text straight into context: Orbit `execution/agent_scheduler.py:~706-830`);
  - there are no attack `pending_injections` (`solvers/orchestrator.py:903-912`);
  - a controller is installed (`runtime.py:300-302`).
  Our scenarios use observation `none` and `delivery="auto"` (`experiments/scenarios.py:365, 678`).
- **A2.** `controlled_delivery` rejects a rewrite that changes anything but content, or a message from another channel (`execution/control.py:461-490`). It does not check an added message's sender or recipients (`control.py:483-486`).
- **A3.** `amend_pending` replaces the current cycle's pending steps wholesale; the current step's remaining agents and later cycles are untouched (`execution/plans.py:255-268`).
- **A4.** Within one batch, the world `scope.problem` sees is the world `apply` sees: message ops write the overlay, never `state.messages`. At a fork, the replay fingerprint (`controller.py:374-379`) makes the child's messages equal the parent's.
- **A5.** One agent runs at a time: explicit plans with concurrent steps are refused (`controller.py:190, 601-610`); the other schedulers are sequential (`agent_scheduler.py:83`).
- **A6.** No `ScheduleConfig.after_activation` callback (it can change the schedule outside ops: `agent_scheduler.py:147-154, 344`).
- **A7.** Policies only return ops; the op set is the 13 registered at `ops.py:481-484`.

## Findings

- **G1. `amend_pending` under-reports `affected`** (confirmed in code). Under `agents={"alice"}` with `amend_pending` allowed, `AmendPending(steps=(("alice",),))` passes the check and replaces every other agent's pending turns in the cycle. The scripted-helper path is guarded (`experiments/l2/validity.py:83-89` forces `agents=None` for activations). Still open: an `l1` inner with a custom scope (`experiments/l2/brief.py:274-291`) and a direct `audit_task(scope=...)`; the LLM auditor offers `amend_pending` whenever the scope allows it (`auditor/llm.py:203-207`).
- **G2. An empty `agent` bypasses the agents limit** (reproduced, mock run). `agents()` is `(agent,) if agent else ()` and `agent: str` has no `min_length` (`ops.py:77-78, 95, 188, 219`). Both agent checks in `_problem` are skipped when `agents()` is empty (`controller.py:533, 537`), and apply sites act on the boundary's agent. Under `agents={"pm"}`, `ReplaceGeneration(agent="", content=...)` at the coder's generation is applied: the coder's model is not called and the coder outputs the auditor's text (ledger `agents=()`). Same for `stage_input` and the three tool ops. Reachable by the LLM auditor via rollback to another agent's boundary (`list_checkpoints` shows them). Fix: `agent: str = Field(min_length=1)`, and check `affected` against the boundary's agent rather than `op.agent`.

## Not gaps

- **N1. Unknown message id.** The scope check falls back to `()`, but `apply` uses the same world (A4) and raises `OpError` for a missing message (`ops.py:286-288`). Injected messages are not in `world.messages`, so they can't be rewritten or dropped. `recipients=()` is a consistent no-op.
- **N2.** `_ExistingMessageOp.affected` drops the sender even from explicit recipients. Harmless: `_target` rejects recipients outside `m.recipients` (`ops.py:290-292`), which never holds the sender (A1).
- **N3.** `Rollback.affected` uses interventions' `agents()`, which miss implicit recipients. The recursion in `scope.py:66-70` catches them, and the child re-checks at the fork. Side effect: the ledger records `agents=()` for such a rollback (`controller.py:572`), an evidence issue.
- **N4. Nested rollbacks** to a revision after the parent's fork always desync: every branch's ids start at prefix 0 (`branches.py:128-136`, `controller.py:380-381`). Fails safe (`ReplayDesync`); the replay model should not assume they succeed.

## Decisions for the theorem statements (before step 3)

- **D1. `halt`.** It ends every agent's remaining turns but lists no agents, and the L1 seat brief relies on it (`experiments/scenarios.py:221-224`: `agents={seat}` with `halt`). Options: exempt from the agents limit by definition; or `affected` = all agents with an explicit carve-out.
- **D2. What a "delivery" is.** Two consistent choices:
  - (a) Only overlay changes count. Then generation ops that post (`tests/contract/test_scope.py:31-33, 62-69` relies on this: the seat's post flips the reviewer) and tool ops on channel tools are fine as they are.
  - (b) Anything that changes what another agent reads counts. Then posting via a generation op, blocking/simulating `channel_post`, and overriding/simulating `channel_read` must count the recipients, or the forged sender, as affected, which breaks the seat brief.
  Currently the code mixes them: `rewrite_message(recipients=("alice",))` counts the sender, while an equivalent `override_tool_result` on Alice's `channel_read` does not.
- **D3. Turns.** Add "whose turns it adds, removes or reorders" to the definition? It's needed for G1 to be a violation of property 2, and interacts with D1.

## Lean model boundary

- **Model:** `AuditScope.allows_op` / `problem`; `agents` / `affected` per op, keeping the `if agent` truthiness test and `None` vs empty exactly; a direct-effect function per op, keyed on the **boundary's** agent; `_decide`'s scope check, check-time all-or-nothing, and per-op apply-time rejection; replay and fork re-checks.
- **Abstract:** other validity checks (boundary, stale revision, unknown agents, budget, approver) as one predicate that can only add rejections; LLM output as an arbitrary function; Orbit via A1–A7.
- **Out:** tool-schema validation, evidence content, budget arithmetic, nested-rollback replay.
