# Lean verification of audit scope: plan

**Goal:** prove in Lean 4 that `AuditScope` enforcement in `src/orbit_audit/` does what it claims: the policy can only apply ops it is allowed to, and only to agents it is allowed to affect. Then check the Lean model against the Python code with differential tests.

**Approach** (after [Kiro, *Harness properties in Lean*](https://kiro.dev/blog/harness-properties-in-lean/)): a Lean model that mirrors the real code, small local theorems, differential tests as the link to Python. The full information-flow theorem (influence only spreads through in-scope ops) is out of scope for now.

## Code covered

- `scope.py`: `AuditScope.allows_op`, `AuditScope.problem` (including recursion into rollback interventions).
- `ops.py`: `agents()` / `affected(world)` for every `ControlOp`, plus what each op's `apply` changes.
- `controller.py`: `_problem`, `_rollback_problem` and the apply path. Every applied op must pass the scope check, whether live, replayed or a rollback intervention.
- Out of the trusted path: `LLMAuditor._dispatch` (overridden in `experiments/`). It sits before the controller, so the controller's check still covers it. Confirm this.

## Properties

1. **Check is sound.** If `problem(op, world) = none`, then `op.name ∈ ops` (or `ops = none`) and `affected(op, world) ⊆ agents` (or `agents = none`), recursively for rollback interventions.
2. **`affected` is complete.** Every agent whose generation, context, deliveries or turns an op's `apply` changes is in `affected(op, world)`. This is what makes property 1 mean something.
3. **Enforcement.** The controller never applies an op for which `problem` is not `none`: not live, not on replay, not as a rollback intervention.
4. **Narrowing is monotone.** If scope `s ⊆ s'`, everything `s` allows, `s'` allows. Combining two scopes by intersection allows exactly what both allow. Needed before L2's helper scope is combined with an inner L1 scope.

## Things to check first (possible gaps, not findings yet)

- `AmendPending.agents()` lists only agents in the *new* steps. Dropping an agent's pending turns does not put that agent in `affected`. If so, `agents={"alice"}` allows removing everyone else's turns (property 2).
- `Halt` has no agents, so it passes any `agents` limit while affecting everyone.
- `_ExistingMessageOp.affected` with `recipients=None` and an unknown message id falls back to `()`. Check that `apply` rejects it in every path.
- Representations to keep exact in the model: `None` (no limit) vs empty set (nothing); op names as strings; `recipients=None` meaning all intended recipients minus the sender.

## Steps

1. **Read and map.** List every op, its `agents`/`affected`, its effects, and every site that applies an op. Write the file:line map.
2. **Lean model.** Lean 4, no Mathlib, in `lean/`. One definition per Python function, each with a file:line header. LLM generation is an arbitrary function (no assumptions about outputs).
3. **Statements.** Write the four theorems. A teammate reviews the statements before any proof work.
4. **Proofs.** For each theorem: a vacuity check (a concrete input meets its assumptions); `#print axioms` shows no `sorry` and no `native_decide`. When stuck on a concrete input, state a `bug_…` theorem.
5. **Differential tests.** The compiled Lean model reads JSON cases and prints verdicts. A pytest + Hypothesis harness feeds the same random scopes, ops and worlds to the real `AuditScope.problem` / `affected` and compares. Generators target edge cases (`None` vs empty, implicit recipients, nested rollbacks). Check that the harness catches a deliberately broken copy of the code.
6. **Findings.** A counterexample counts only once it reproduces on the unmodified code and a real scenario or brief can build that scope. Fix it in a separate PR.
7. **CI.** Run `lake build` and the differential tests on changes to `src/orbit_audit/`.

## Later

- The full information-flow theorem.
- Rollback combined with scope: out-of-scope effects never survive a branch.
