import Scope.Spec.BranchStatements
import Scope.Spec.Witnesses

/-!
# Rollback witnesses (frozen)

Concrete audits, checked by evaluation in `Proofs/BranchSanity.lean`:

* `RealForkWitness` — the statements' hypotheses can hold at once on a real fork: the child
  replays an applied op, applies its intervention at the fork and completes.
* `bug_ForkRejected` (RB1) — an accepted rollback whose child rejects its intervention at
  the fork (it names another agent than the fork boundary's) and completes as a no-op.
* `bug_NestedDesync` (RB3) — a rollback nested past its parent's fork: an id minted between
  the two forks is fingerprinted, so the grandchild desyncs and is not scored.
* `bug_NestedSilent` (RB3) — the same with a fingerprint that does not show the id: the
  grandchild passes its parent's boundary with different state, and mints the same id as
  branch 0 (property 6 fails).
* `ScopedForkWitness` — the real fork under a scope of Alice alone (`ForkEffect`'s setting):
  Bob's own view is untouched.
* `VetoWitness` — the approver, reading the shared state, vetoes the parent's first batch:
  the parent records no decisions there (M1), and the child, starting from a different
  shared state, applies nothing there either; it applies its intervention at the fork.
* `SameForkWitness` — a parent that is itself a child forks its own child at the same
  revision (`NotNestedAbove` with equality): no desync.
* `T8Witness` — the witnesses' envs satisfy T8, and branch 0 of the real fork queues a
  child forking at 2, so `Fidelity`'s and `ForkEffect`'s hypotheses hold together there.

Orbit here mints one id per step and keeps the last one in `hidden`; only that minted id,
never the id provider itself, reaches the fingerprint.
-/

namespace Scope.Branch.Witness

open Scope.Witness (w0)

def s0 : State := { world := w0 }

/-- Every boundary is Alice's generation; Orbit's step mints an id, recorded in `hidden` as
`tag * 100 + count`; the run completes after five boundaries. -/
def mkEnv (fp : State → Nat) : Env where
  ctx := fun _ => ⟨.generation, 0, some "alice", none⟩
  fingerprint := fp
  gen := fun b rev _ => b * 10 + rev
  step := fun _ _ _ st =>
    { world := st.world, ids := { tag := st.ids.tag, count := st.ids.count + 1 },
      hidden := st.ids.tag * 100 + st.ids.count }
  done := fun _ rev _ => Decidable.decide (rev > 5)
  cut := fun _ _ _ _ => none
  stale := fun _ _ _ => none
  applyOk := fun _ _ _ _ => true
  overBudget := fun _ _ _ => false
  veto := fun _ _ _ _ => false
  rollbackExtra := fun _ _ _ _ _ => none
  notice := fun sh _ _ _ es => sh + es.length
  begin := fun sh _ => sh

/-- The fingerprint shows the last minted id. -/
def env : Env := mkEnv (·.hidden)
/-- The fingerprint shows nothing that differs between branches. -/
def blindEnv : Env := mkEnv (fun _ => 0)

def says (who text : String) : Op := .replaceGeneration who text []

/-- Summary of a branch: how it ended, and its applied ops as (revision, op, source). -/
def summary (c : Cfg) : Option Status × List (Nat × String × Source) :=
  (c.ended, c.entries.map (fun e => (e.revision, e.op.name, e.source)))

/-- Branch 0 writes Alice's first generation, then at revision 3 rolls back to 2 with an
intervention on Alice's generation there. -/
def forkPolicy : Policy := fun b rev _ _ =>
  if b == 0 && rev == 1 then [says "alice" "first"]
  else if b == 0 && rev == 3 then [.rollback 2 [says "alice" "pressure"]]
  else []

def RealForkWitness : Prop :=
  (runAudit none env forkPolicy s0 0 3 10).map summary =
    [(some .rolledBack, [(1, "replace_generation", .live), (3, "rollback", .live)]),
     (some .completed, [(1, "replace_generation", .replay), (2, "replace_generation", .fork)])]

/-- As `forkPolicy`, but the intervention names Bob while revision 2 is Alice's generation:
`_rollback_problem` accepts it (it only checks the boundary kind), the child rejects it. -/
def wrongAgentPolicy : Policy := fun b rev _ _ =>
  if b == 0 && rev == 3 then [.rollback 2 [says "bob" "pressure"]] else []

def bug_ForkRejected : Prop :=
  (runAudit none env wrongAgentPolicy s0 0 3 10).map summary =
    [(some .rolledBack, [(3, "rollback", .live)]), (some .completed, [])]

/-- Branch 0 rolls back at 4 to 2; branch 1 (fork 2) rolls back at 5 to 4, past its own fork. -/
def nestedPolicy : Policy := fun b rev _ _ =>
  if b == 0 && rev == 4 then [.rollback 2 []]
  else if b == 1 && rev == 5 then [.rollback 4 []]
  else []

def bug_NestedDesync : Prop :=
  (runAudit none env nestedPolicy s0 0 3 10).map (·.ended) =
    [some .rolledBack, some .rolledBack, some .replayDesync] ∧
  -- the grandchild forks at 4 but desyncs at boundary 3, before its fork
  ((runAudit none env nestedPolicy s0 0 3 10)[1]?.bind childReplay).map (·.fork) = some 4 ∧
  (runAudit none env nestedPolicy s0 0 3 10).map (·.rev) = [4, 5, 3] ∧
  scored (runAudit none env nestedPolicy s0 0 3 10) = none

/-- The branches of the nested audit under `blindEnv`: branch 0, its child, its grandchild's
replay. -/
def blind0 : Cfg := branch none blindEnv nestedPolicy 0 none 10 0 s0
def blind1 : Cfg := branch none blindEnv nestedPolicy 1 (childReplay blind0) 10 0 s0

/-- With a fingerprint blind to ids, the grandchild completes, yet after three boundaries
its state differs from its parent's there: the parent minted ids with its own tag after its
fork at 2, the grandchild with tag 0. -/
def bug_NestedSilent : Prop :=
  (runAudit none blindEnv nestedPolicy s0 0 3 10).map (·.ended) =
    [some .rolledBack, some .rolledBack, some .completed] ∧
  (run none blindEnv nestedPolicy 2 (childReplay blind1) 3 (start blindEnv 0 2 s0)).ended
    = none ∧
  (run none blindEnv nestedPolicy 2 (childReplay blind1) 3 (start blindEnv 0 2 s0)).st.hidden ≠
    (run none blindEnv nestedPolicy 1 (childReplay blind0) 3 (start blindEnv 0 1 s0)).st.hidden ∧
  (run none blindEnv nestedPolicy 2 (childReplay blind1) 3 (start blindEnv 0 2 s0)).st.hidden =
    (run none blindEnv nestedPolicy 0 none 3 (start blindEnv 0 0 s0)).st.hidden

def ScopedForkWitness : Prop :=
  (runAudit (some Scope.Witness.seat) env forkPolicy s0 0 3 10).map summary =
    [(some .rolledBack, [(1, "replace_generation", .live), (3, "rollback", .live)]),
     (some .completed, [(1, "replace_generation", .replay), (2, "replace_generation", .fork)])] ∧
  (runAudit (some Scope.Witness.seat) env forkPolicy s0 0 3 10).map (·.st.world.own "bob") =
    [[], []]

/-- The approver vetoes any non-empty batch while the shared count is 0; every boundary adds
one to it. -/
def vetoEnv : Env :=
  { env with veto := fun sh _ ops _ => sh == 0 && !ops.isEmpty,
             notice := fun sh _ _ _ _ => sh + 1 }

def VetoWitness : Prop :=
  (runAudit none vetoEnv forkPolicy s0 0 3 10).map summary =
    [(some .rolledBack, [(3, "rollback", .live)]),
     (some .completed, [(2, "replace_generation", .fork)])] ∧
  (runAudit none vetoEnv forkPolicy s0 0 3 10).map (fun c => c.steps.map (·.decisions.length)) =
    [[0, 0, 1], [0, 1, 0, 0, 0]]

/-- Branch 0 rolls back at 3 to 2; branch 1 (fork 2) rolls back at 4 to 2 again. -/
def sameForkPolicy : Policy := fun b rev _ _ =>
  if b == 0 && rev == 3 then [.rollback 2 []]
  else if b == 1 && rev == 4 then [.rollback 2 []]
  else []

def SameForkWitness : Prop :=
  (runAudit none env sameForkPolicy s0 0 3 10).map (·.ended) =
    [some .rolledBack, some .rolledBack, some .completed]

def T8Witness : Prop :=
  T8 env ∧ T8 blindEnv ∧ T8 vetoEnv ∧
  (childReplay (branch none env forkPolicy 0 none 10 0 s0)).map (·.fork) = some 2

end Scope.Branch.Witness
