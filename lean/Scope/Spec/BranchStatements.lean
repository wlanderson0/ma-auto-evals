import Scope.Spec.Branch
import Scope.Spec.Statements

/-!
# The rollback statements (frozen; rollback plan step 3)

Each property of `docs/superpowers/plans/2026-11-17-lean-rollback.md` is a `Prop` here,
proved in `Scope/Proofs/Branch*.lean`. Assumptions are hypotheses, named after
`lean/rollback-map.md`:

* `T8` — Orbit, the agents and pure tools are deterministic given the outputs served, and
  so is whether Orbit's run is complete: `Env.step` and `Env.done` ignore the branch index.
  Live model outputs (`Env.gen`), limits (`Env.cut`) and everything shared may differ.
* `NotNestedAbove` — the child's fork is not past its parent's own fork (RB3 is the case
  this excludes).
* `TagKept` — only `diverge` changes the id tag (`IdsOwnAfterFork`).
* `World.WF` — unique message ids, as in the scope statements (`ForkEffect`,
  `ScopeRespectedB`).
* A7 — policies only return ops (RB6): built into `Policy`.

Not stated: the plan's property 5 (a no-op rollback is the identity up to ids). It would
need every `Env` reader and the policy to ignore the id tag after the fork, which the Python
does not do (ids are fingerprinted and shown to agents), and `Env.gen` to ignore the
branch; it would restate determinism. The R7 test covers it by renumbering ids; it is not
claimed in Lean. RB1 and RB3 (property 6's failure) are witnesses (`BranchWitnesses.lean`):
`bug_NestedDesync` runs in an env that satisfies T8 (`T8Witness`) and desyncs before its
fork, which is why the statements need `NotNestedAbove`.

Decisions: D1 (`halt` exempt, `carvedOut`), D4 (a rollback may rewind agents outside the
scope: `ForkEffect` states what they see), D5 (`scored`).
-/

namespace Scope.Branch

/-- T8 (rollback-map): given the output served, Orbit's step and its completion do not
depend on the branch. -/
def T8 (env : Env) : Prop :=
  (∀ b b' rev o st, env.step b rev o st = env.step b' rev o st) ∧
  (∀ b b' rev st, env.done b rev st = env.done b' rev st)

/-- The child the audit loop queues for a branch that rolled back (runner.py:135-140). -/
def childReplay (parent : Cfg) : Option Replay :=
  parent.rollback.map fun (t, ivs) =>
    { steps := parent.steps.take t, fork := t, interventions := ivs }

/-- The child's fork is at or before its parent's own fork, if the parent has one. -/
def NotNestedAbove (rp : Option Replay) (R : Replay) : Prop :=
  ∀ r, rp = some r → R.fork ≤ r.fork

/-- An entry without the fields that name its branch. -/
def Entry.erase (e : Entry) : Entry := { e with branch := 0, source := .live }

/-- `c` has not ended, or ended through a limit or an error: never a desync, never as
completed or rolled back. -/
def CutOrRunning (c : Cfg) : Prop :=
  c.ended = none ∨ ∃ x : Cut, c.ended = some x.status

/-- Property 1, replay fidelity. Under T8, and unless the child's fork is past its parent's,
at every boundary before the fork the child is where its parent was: the same state, and
the same applied ops in the same worlds, whatever the shared state (ledger, approver,
policy memory) has become. It never desyncs there; it can only be cut short by a limit or
an error. -/
def Fidelity : Prop :=
  ∀ (scope : Option AuditScope) (env : Env) (policy : Policy) (p c : Nat)
    (rp : Option Replay) (fuel : Nat) (sh sh' : Shared) (s0 : State) (R : Replay),
    T8 env → NotNestedAbove rp R →
    childReplay (branch scope env policy p rp fuel sh s0) = some R →
    ∀ i, i < R.fork →
      let cc := run scope env policy c (some R) i (start env sh' c s0)
      let pc := run scope env policy p rp i (start env sh p s0)
      CutOrRunning cc ∧
      (cc.ended = none → cc.st = pc.st ∧ cc.rev = pc.rev ∧
        cc.entries.map Entry.erase = pc.entries.map Entry.erase)

/-- Property 2a, fail-safe, for every `Env`: at a boundary up to the fork that the run
reaches (neither done nor cut there) and whose fingerprint differs from the recording, the
branch ends as a desync with nothing applied there. -/
def FailSafe : Prop :=
  ∀ (scope : Option AuditScope) (env : Env) (policy : Policy) (b : Nat) (R : Replay) (c : Cfg),
    c.ended = none → c.rev ≤ R.fork →
    env.done b c.rev c.st = false → env.cut b c.rev c.shared c.st = none →
    (∀ s, R.steps[c.rev - 1]? = some s → s.fingerprint ≠ env.fingerprint c.st) →
    let c' := tick scope env policy b (some R) c
    c'.ended = some .replayDesync ∧ c'.entries = c.entries ∧ c'.st = c.st

/-- Property 2b, for every `Env` (the RB2 fix): a child that completes reached its fork. -/
def CompletedReachedFork : Prop :=
  ∀ (scope : Option AuditScope) (env : Env) (policy : Policy) (b : Nat) (R : Replay)
    (fuel : Nat) (sh : Shared) (s0 : State),
    let o := branch scope env policy b (some R) fuel sh s0
    o.ended = some .completed → R.fork ≤ o.rev - 1

/-- Property 1, at the fork: under the same assumptions, after its fork boundary the child
is still running, has rolled back again, or was cut short by a limit or an error; it never
desyncs or completes there. -/
def ThroughFork : Prop :=
  ∀ (scope : Option AuditScope) (env : Env) (policy : Policy) (p c : Nat)
    (rp : Option Replay) (fuel : Nat) (sh sh' : Shared) (s0 : State) (R : Replay),
    T8 env → NotNestedAbove rp R →
    childReplay (branch scope env policy p rp fuel sh s0) = some R →
    let cc := run scope env policy c (some R) R.fork (start env sh' c s0)
    CutOrRunning cc ∨ cc.ended = some .rolledBack

/-- Property 2c, for every `Env`: every boundary up to the fork that a child got past
matched the parent's recording. -/
def FailSafeRun : Prop :=
  ∀ (scope : Option AuditScope) (env : Env) (policy : Policy) (c : Nat) (R : Replay)
    (n : Nat) (sh : Shared) (s0 : State) (k : Nat),
    1 ≤ k → k ≤ R.fork →
    k < (run scope env policy c (some R) n (start env sh c s0)).rev →
    (R.steps[k - 1]?).map (·.fingerprint) =
      some (env.fingerprint (run scope env policy c (some R) (k - 1) (start env sh c s0)).st)

/-- Property 4, the rollback's own effect (D4). Under property 1's assumptions, a child not
cut short before its fork is, at the fork, in its parent's world at that revision; and if
that world has unique message ids and none of the fork's ops (the interventions and the
woken policy's) is `halt` or `rollback` (D1), every agent outside the scope still sees,
after them, what it saw at that revision of the parent. -/
def ForkEffect : Prop :=
  ∀ (s : AuditScope) (env : Env) (policy : Policy) (p c : Nat) (rp : Option Replay)
    (fuel : Nat) (sh sh' : Shared) (s0 : State) (R : Replay),
    T8 env → NotNestedAbove rp R →
    childReplay (branch (some s) env policy p rp fuel sh s0) = some R →
    let cc := run (some s) env policy c (some R) (R.fork - 1) (start env sh' c s0)
    let pc := run (some s) env policy p rp (R.fork - 1) (start env sh p s0)
    let forkOps := (tick (some s) env policy c (some R) cc).entries.drop cc.entries.length
    cc.ended = none →
      cc.st.world = pc.st.world ∧
      (pc.st.world.WF → (∀ e ∈ forkOps, carvedOut e.op = false) →
        ∃ w', Chain cc.st.world forkOps w' ∧
          ∀ a, s.allowsAgent a = false → view w' a = view pc.st.world a)

/-- Only `diverge` changes the id tag: Orbit's step keeps it. -/
def TagKept (env : Env) : Prop :=
  ∀ b rev o st, (env.step b rev o st).ids.tag = st.ids.tag

/-- Property 6, the positive half: a child's id tag is 0 before its fork and its own index
after it. That ids are then unique across branches is informal: branches have distinct
indices, and branch 0, which never forks, keeps tag 0. -/
def IdsOwnAfterFork : Prop :=
  ∀ (scope : Option AuditScope) (env : Env) (policy : Policy) (c : Nat) (R : Replay)
    (n : Nat) (sh : Shared) (s0 : State), TagKept env → 1 ≤ R.fork →
    let cc := run scope env policy c (some R) n (start env sh c s0)
    (cc.rev < R.fork → cc.st.ids.tag = 0) ∧ (R.fork < cc.rev → cc.st.ids.tag = c)

/-- Scope results in this model (rollback plan step 4): every op applied in any branch,
live, replayed or at a fork, was checked against the audit's scope in the world it changed,
and, in a world with unique message ids and unless it is `halt` or `rollback`, left every
agent outside the scope seeing what it saw. -/
def ScopeRespectedB : Prop :=
  ∀ (s : AuditScope) (env : Env) (policy : Policy) (s0 : State) (sh : Shared)
    (maxBranches fuel : Nat),
    ∀ o ∈ runAudit (some s) env policy s0 sh maxBranches fuel, ∀ e ∈ o.entries,
      e.scope = some s ∧ s.problem e.pre e.op = none ∧
      (e.pre.WF → carvedOut e.op = false →
        ∀ a, s.allowsAgent a = false → view e.post a = view e.pre a)

/-- Property 7a: branches form a chain; every branch but the last rolled back. -/
def ChainShape : Prop :=
  ∀ (scope : Option AuditScope) (env : Env) (policy : Policy) (s0 : State) (sh : Shared)
    (maxBranches fuel : Nat),
    let outs := runAudit scope env policy s0 sh maxBranches fuel
    ∀ i (h : i + 1 < outs.length), outs[i].ended = some .rolledBack

/-- Property 7a': the audit is branch 0 then, for each rollback, the child that replays it:
this is what ties the per-branch statements (1, 2, 4, 6) to an audit. -/
def AuditLink : Prop :=
  ∀ (scope : Option AuditScope) (env : Env) (policy : Policy) (s0 : State) (sh : Shared)
    (maxBranches fuel : Nat),
    let outs := runAudit scope env policy s0 sh maxBranches fuel
    (∀ o, outs.head? = some o → o = branch scope env policy 0 none fuel sh s0) ∧
    ∀ i (h : i + 1 < outs.length),
      outs[i + 1] = branch scope env policy (i + 1) (childReplay outs[i]) fuel outs[i].shared s0

/-- Property 7b (D5): the scored branch is the last one, and only if it completed. -/
def ScoredIffCompleted : Prop :=
  ∀ (outs : List Cfg) (o : Cfg),
    scored outs = some o ↔ (adopted outs = some o ∧ o.ended = some .completed)

/-- Property 7c, for every `Env`: in an audit, a scored child completed past its fork. -/
def ScoredReachedFork : Prop :=
  ∀ (scope : Option AuditScope) (env : Env) (policy : Policy) (s0 : State) (sh : Shared)
    (maxBranches fuel : Nat),
    let outs := runAudit scope env policy s0 sh maxBranches fuel
    ∀ o, scored outs = some o →
      o.ended = some .completed ∧
      ∀ i (h : i + 1 < outs.length) (R : Replay), outs[i + 1] = o →
        childReplay outs[i] = some R → R.fork < o.rev

end Scope.Branch
