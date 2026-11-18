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
* `IdBlind` (T10) — what Orbit does, and what the fingerprint and view read, ignores the
  id tag. False in the Python (ids are fingerprinted); the R7 test assumes it by renumbering.
* A7 — policies only return ops (RB6): built into `Policy`.

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
      let cc := run scope env policy c (some R) i { shared := sh', st := s0 }
      let pc := run scope env policy p rp i { shared := sh, st := s0 }
      CutOrRunning cc ∧
      (cc.ended = none → cc.st = pc.st ∧ cc.rev = pc.rev ∧
        cc.entries.map Entry.erase = pc.entries.map Entry.erase)

/-- Property 2a, fail-safe, for every `Env`: at a boundary up to the fork whose fingerprint
differs from the recording, the branch ends as a desync with nothing applied there. -/
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

/-- Property 4, the rollback's own effect (D4). Under property 1's assumptions, an agent
outside the scope is, at the fork, where it was at that revision of the parent; the fork's
ops (interventions and the woken policy's) then leave its view unchanged, unless one is
`halt` (D1). -/
def ForkEffect : Prop :=
  ∀ (s : AuditScope) (env : Env) (policy : Policy) (p c : Nat) (rp : Option Replay)
    (fuel : Nat) (sh sh' : Shared) (s0 : State) (R : Replay),
    T8 env → NotNestedAbove rp R →
    childReplay (branch (some s) env policy p rp fuel sh s0) = some R →
    let cc := run (some s) env policy c (some R) (R.fork - 1) { shared := sh', st := s0 }
    let pc := run (some s) env policy p rp (R.fork - 1) { shared := sh, st := s0 }
    let forkOps := (tick (some s) env policy c (some R) cc).entries.drop cc.entries.length
    cc.ended = none →
      cc.st.world = pc.st.world ∧
      (∀ e ∈ forkOps, e.pre.WF → carvedOut e.op = false →
        ∀ a, s.allowsAgent a = false → view e.post a = view e.pre a) ∧
      Chain cc.st.world forkOps
        ((forkOps.getLast?.map (·.post)).getD cc.st.world)

/-- Scope results in this model (rollback plan step 4): every op applied in any branch,
live, replayed or at a fork, was checked against the audit's scope in the world it changed,
and unless it is `halt` or `rollback` left every agent outside the scope seeing what it saw. -/
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

/-- Property 7b (D5): the scored branch is the last one, and only if it completed. -/
def ScoredIffCompleted : Prop :=
  ∀ (outs : List Cfg) (o : Cfg),
    scored outs = some o ↔ (adopted outs = some o ∧ o.ended = some .completed)

end Scope.Branch
