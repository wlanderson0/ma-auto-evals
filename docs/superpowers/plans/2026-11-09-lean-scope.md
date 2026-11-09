# Lean verification of audit scope: plan

**Goal:** prove in Lean 4 that `AuditScope` enforcement in `src/orbit_audit/` does what it claims: the policy can only apply ops it is allowed to, and those ops leave every agent outside its `agents` limit untouched. Then check the Lean model against the Python code with differential and structural tests.

**Approach** (after [Kiro, *Harness properties in Lean*](https://kiro.dev/blog/harness-properties-in-lean/) and Cedar's verification-guided development): a Lean model that mirrors the real code, small local theorems, differential tests as the link to Python. The result is a two-tier claim: theorems proved about the model at a pinned commit, and agreement between model and Python that is tested, not proved. The full information-flow theorem (influence only spreads through in-scope ops) is out of scope for now, but the statements below are shaped so it can reuse them. Background and sources: [the research review](2026-11-09-lean-scope-review.md).

## Code covered

- `scope.py`: `AuditScope.allows_op`, `AuditScope.problem` (including recursion into rollback interventions).
- `ops.py`: `agents()` / `affected(world)` for every registered `ControlOp`, plus what each op's `apply` changes.
- `controller.py`: `_decide`, `_problem`, `_rollback_problem` and every apply site. Every applied op must pass the scope check, whether live, replayed or a rollback intervention at a fork.
- Not trusted, but not modelled either: the policy itself (`LLMAuditor._dispatch`, scripted policies, `experiments/` overrides). The controller's check covers whatever they return, provided they cannot reach the world or the controller another way (see T1).

The step 1 map is `lean/scope-map.md` (A1–A7, findings G1–G2, N1–N4, decisions D1–D3).

## Properties

The model has one world-changing primitive, `applyRaw`, reachable only through `applyChecked`, which logs every applied op as `(op, source, preWorld)` with `source ∈ {live, replay, fork}`. `view a w` is a specification definition (no Python counterpart): everything an op can directly change for agent `a`, per D2 and D3. It is defined from the world state, never from `affected`.

1. **Check is sound.** If `problem(op, w) = none`, then `op.name ∈ ops` (or `ops = none`) and `affected(op, w) ⊆ agents` (or `agents = none`). For rollback, each intervention is checked recursively in the world at the rollback (`_rollback_problem`), and the controller checks it again in the fork's world before applying it (property 3).
2. **Frame.** If `apply op w = ok w'` and `a ∉ affected(op, w)`, then `view a w' = view a w`. This is `affected` completeness stated as a frame condition (seL4 integrity, Nickel's local respect). It makes G1 and G2 fail the theorem rather than depend on inspection.
3. **Enforcement.** For every policy, every LLM generation function and every run of the model, each log entry `(op, source, pre)` has `problem(scope, op, pre) = none`. The check uses the world `apply` consumes (A4), with a fresh check on replay and at a fork.
4. **Narrowing is monotone.** If scope `s ≤ s'`, everything `s` allows, `s'` allows. `allows (s ⊓ t) ↔ allows s ∧ allows t`, including through the rollback recursion. Plus: a rejected op (at check time or at apply time) leaves every view unchanged, which is what lets an arbitrary auditor under a wider scope do everything one under a narrower scope can.
5. **Confinement.** No step (live, replay, fork) changes the scope, and any scope given to a helper or nested auditor is `≤` its parent's. Without this, property 4 is a fact about static values only.

One headline theorem, `scope_respected`, composes 1–3 and 5, so the published claim maps to a single Lean name. LLM generation is a universally quantified function of an agent's view and an explicit oracle/seed argument, never an `axiom`.

## Decisions before the statements

From the step 1 map, settled before step 3 and recorded in the assurance statement:

- **D1 `halt`.** Default: `affected halt = all agents`, so `agents={seat}` forbids it. If the L1 seat brief needs it, a named carve-out in the headline theorem (`op ≠ halt ∨ halt ∈ scope.ops`, with a line in the claim), never an accidental pass.
- **D2 deliveries.** Choose (a) overlay changes only, or (b) anything another agent reads. The definition of `view` encodes the choice. Under (a), a message an in-scope agent posts is that agent's own output, and its spread is for the later information-flow theorem.
- **D3 turns.** Include each agent's pending turns, with order and position, in `view`. Needed for G1 to be a violation.
- **G2 empty `agent`.** Check `affected` against the boundary's agent, not `op.agent`, before modelling, or model the current code and keep the `bug_` theorem.
- **Scope shape.** A product of an op set and an agent set cannot say "this op only on this agent". If the L2 + L1 combination needs that, the scope becomes a relation and property 4 changes.

## Still to check

- Op values are not mutated between check and apply (frozen models or a copy at check time). The Lean model cannot show this.
- The rollback's own effect: every agent whose view reverts at the fork is checked, not only the interventions' agents.
- Whether rejections are visible to agents (error messages, renumbered indices). If they are, property 4's rejection lemma fails and they are an information channel.
- What does not reach a model: indices, schedule positions and controller feedback that agents can see.

## Steps

1. **Read and map.** Done: `lean/scope-map.md`, and the AST inventory backing T1 (`lean/scope-inventory.json`, checked by `tests/structural/test_scope_inventory.py`).
2. **Lean model.** Done: `lean/Scope/Spec/{Model,View,Run}.lean`. Lean 4 (pinned 4.35.0), core and `Std` only, no Mathlib, in `lean/`. Two kinds of definition:
   - *mirror*: one per Python function, each with a file:line header, keeping `None` vs empty and the `if agent` truthiness test exactly;
   - *specification*: `view`, the log, `changedAgents`, each with a short prose justification.
   Scopes as `Option (List String)` with `Bool` membership, `≤` defined by membership, not list equality. Ops as an inductive with `Op.name` returning the Python strings. Recursion into rollback interventions is structural (mutual over `Op` and `List Op`), so concrete checks close with `decide`.
3. **Statements.** Mirror definitions, specification definitions and the five theorems (ending in `sorry`) go in a frozen `Spec.lean`. A teammate reviews it before any proof work and writes some of the sanity and counterexample theorems. CI records its hash and fails if it changes without re-review. Proofs never edit it.
4. **Proofs.** Gate: the axioms of every theorem ⊆ {`propext`, `Classical.choice`, `Quot.sound`} (an allow-list: native computation shows up as `_native` axioms since 4.29, not as `native_decide`). Run `lean4checker`, and comparator if the pinned toolchain has it. Proof files may not use `notation`, `instance`, `macro`, `elab`, `set_option debug.*`, `implemented_by`, `extern`, `partial` or `unsafe`. Each theorem gets, in `Sanity.lean`:
   - a satisfiability witness with a strict, non-empty `affected`;
   - a precision witness: an in-scope op is accepted and applied, and `affected` is exactly the expected set;
   - refutations on mutated models (`problem` ignoring `agents`, `affected` from new steps only, a controller skipping the replay check);
   - representation witnesses: `none` vs `some []`, `recipients=None`, nested rollback.
   When stuck on a concrete input, state a `bug_…` theorem; after a fix, a `fixed_…` theorem beside it.
5. **Differential and structural tests.** The compiled Lean model reads JSON Lines and prints verdicts and effects. The JSON codec is written by hand, with golden round-trips keeping `null`, `[]` and a missing field distinct. A pytest + Hypothesis harness compares Lean and Python on:
   - `problem` and `affected` verdicts;
   - `apply` effects: the per-agent view diff before and after, against both the Lean effect function and Python's `affected`;
   - generated episodes with replay and rollback, with a runtime assertion at the apply primitive that `problem(op, world) is None` for the world actually passed in.
   Two generators: world-first (ops refer to the world's agents and message ids) and ill-formed (unknown ids, unknown agents, empty `agent`). Plus a saved corpus of `@example`s and ops replayed from real experiment runs. Structural tests: every `ControlOp` subclass has a model entry; the apply-site inventory matches the map.
   Adequacy, reported with the results: mutation kill rate on the Python (targeted mutants for G1, G2, D1, `None` as empty, a replay path skipping the check), mutants of the Lean model refuted, and the distribution of verdict classes from Hypothesis `event()`.
6. **Findings.** A counterexample must reproduce on unmodified code. Classify it as reachable (a real scenario or brief builds the scope), latent (reproduces, no brief builds it yet) or model-only. Fix in a separate PR.
7. **CI.** `leanprover/lean-action` with `--wfail` and `lean4checker`; the axiom allow-list; the `Spec.lean` hash; a file:line staleness check; triggered by changes to `src/orbit_audit/**` or `lean/**`. A fast fixed-seed Hypothesis profile on PRs, a longer one nightly.
8. **Assurance statement.** `lean/ASSURANCE.md`, pinned to a commit, toolchain and `Spec.lean` hash: the one-sentence claim (`scope_respected`, "about the model; agreement with the Python is tested, not proved"), what is proved, how the model is tied to the Python (with counts), what is assumed, what is not covered, open decisions and findings, counts. One traceability row per theorem: English claim, Lean name, specification definitions used, Python file:line, differential target, witnesses, axioms, reviewer. Template in the review.

## Assumptions

The step 1 map's A1–A7 (Orbit delivery, rewrite checks, `amend_pending` semantics, check world = apply world, one agent at a time, no `after_activation` callback, the 13 registered ops), plus:

- **T1.** No code outside the apply sites changes the world. At `b7b1eeb` this holds by convention only (`BranchQueries._controller`, `on_controller`). Backed by the AST inventory and the runtime assertion; an out-of-process auditor would make it architectural.
- **T2.** Policy code cannot change the scope or the controller's reference to it.
- **T3.** Op values are not mutated between check and apply.
- **T4.** The Python agrees with the model on `problem`, `affected`, `apply` effects and apply sites. Tested, not proved.
- **T5.** Lean's kernel and the three standard axioms are sound. The Lean compiler is trusted for the differential-test executable only.
- **T6.** Results hold at the pinned commit; CI reruns on change.

Not permissible, since each would empty the claim: any assumption about which ops the policy proposes; reusing recorded verdicts on replay; treating `halt` or an empty `affected` as harmless; treating the transcript as a record of what was applied.

## Not covered

Information flow beyond one op; effects surviving a branch; faithful logs and transcripts; LLM behaviour beyond "arbitrary function"; Orbit and Inspect internals beyond A1–A7; concurrency beyond A5; performance and denial of service.

## Later

- **Information flow.** Intransitive noninterference (Rushby): the auditor flows only to in-scope agents, agents to each other only through deliveries. Property 2 is its local-respect obligation and property 4's rejection lemma its step for rejected ops; what remains is step consistency for ordinary turns and output consistency (each agent's generation reads only its own view). Needed to catch an out-of-scope effect built from several in-scope ops.
- **Rollback with scope.** Out-of-scope effects never survive a branch. Smaller once the fork's own effect is checked.
- **Log completeness.** Every applied op is logged, and the log is a function of the applied ops.
- **Proved code as the enforcement point.** `AuditScope.problem` calls the compiled Lean checker, so T4 for `problem` and `affected` becomes proved modulo the Lean compiler.
