import Scope.Proofs.Branch

/-! One boundary of `tick`, with its intermediate values named, and how its results move
`rev`, `steps` and `rollback`. -/

namespace Scope.Branch

section
variable (scope : Option AuditScope) (env : Env) (policy : Policy) (b : Nat)
  (replay : Option Replay) (c : Cfg)

def tSt : State := if forkAt replay c.rev then { c.st with ids := c.st.ids.diverge b } else c.st
def tCtx : Ctx := { env.ctx c.st with revision := c.rev }
def tOps : List Op := batchAt policy b replay c.rev c.shared c.st
def tOver : Bool :=
  if replayingAt replay c.rev then ((recordedAt replay c.rev).map (·.overBudget)).getD false
  else env.overBudget c.shared (tCtx env c) (tOps policy b replay c)
def tProblems : Bool :=
  (tOps policy b replay c).any (fun op => (checkProblem scope (tCtx env c) op (tSt b replay c).world).isSome)
    || ((tOps policy b replay c).filter (fun op => op.family == familyAt (tCtx env c).boundary)).length > 1
      && soleBoundary (tCtx env c).boundary
    || (env.stale (tCtx env c) (tOps policy b replay c) (tSt b replay c).world).isSome
def tVetoed : Bool :=
  !(tOps policy b replay c).any Op.isRollback && !replayingAt replay c.rev
    && !tProblems scope env policy b replay c && !tOver env policy b replay c
    && env.veto c.shared (tCtx env c) (tOps policy b replay c) c.st
def tOut : Output :=
  if replayingAt replay c.rev then ((recordedAt replay c.rev).map (·.output)).getD 0
  else env.gen b c.rev (tSt b replay c)
def tHooks : Hooks :=
  { extra := fun ctx' ops' w' =>
      if ops'.any Op.isRollback then env.rollbackExtra b c.shared ctx' ops' w'
      else match env.stale ctx' ops' w' with
        | some r => some r
        | none => if tOver env policy b replay c || tVetoed scope env policy b replay c
            then some "rejected" else none,
    applyOk := env.applyOk (tOut env b replay c) }
def tMk : Op → World → World → Entry := fun op pre post =>
  { branch := b, revision := c.rev, source := sourceOf replay c.rev, scope := scope,
    ctx := tCtx env c, op := op, pre := pre, post := post }
def tBAt : Nat → Option Boundary := fun t => (c.steps.map (·.boundary) ++ [(tCtx env c).boundary])[t - 1]?
def tRecorded : List Op :=
  if tVetoed scope env policy b replay c then [] else tOps policy b replay c
def tStep (out : Output) : Step :=
  ⟨(tCtx env c).boundary, env.fingerprint c.st, tRecorded scope env policy b replay c,
   tOver env policy b replay c, out⟩
def tDecide : Outcome :=
  decide scope (tHooks scope env policy b replay c) (tMk scope env b replay c) (tCtx env c)
    (replayingAt replay c.rev) (tBAt env c) (tOps policy b replay c) (tSt b replay c).world

theorem tick_body (hc : c.ended = none) :
    tick scope env policy b replay c =
      match env.cut b c.rev c.shared c.st with
      | some x => { c with ended := some x.status }
      | none =>
        if env.done b c.rev c.st then { c with ended := some (finished replay c.rev) }
        else if desynced env replay c.rev c.st then { c with ended := some .replayDesync }
        else match tDecide scope env policy b replay c with
          | .rollback es t ivs =>
            { c with st := tSt b replay c,
                     shared := env.notice c.shared b c.rev (tOps policy b replay c) es,
                     steps := c.steps ++ [tStep scope env policy b replay c 0],
                     entries := c.entries ++ es, ended := some .rolledBack,
                     rollback := some (t, ivs) }
          | .next w es =>
            { rev := c.rev + 1,
              shared := env.notice c.shared b c.rev (tOps policy b replay c) es,
              st := env.step b c.rev (tOut env b replay c) { tSt b replay c with world := w },
              steps := c.steps ++ [tStep scope env policy b replay c (tOut env b replay c)],
              entries := c.entries ++ es, ended := none, rollback := none } := by
  simp only [tick, hc]
  rfl
end

theorem decide_rollback {scope : Option AuditScope} {hooks : Hooks}
    {mk : Op → World → World → Entry} {ctx : Ctx} {replaying : Bool}
    {bAt : Nat → Option Boundary} {ops : List Op} {w : World} {es : List Entry} {t : Nat}
    {ivs : List Op} (h : decide scope hooks mk ctx replaying bAt ops w = .rollback es t ivs) :
    t ≠ 0 ∧ t ≤ ctx.revision := by
  unfold decide at h
  split at h
  · split at h
    · cases h
    · split at h
      · rename_i op
        split at h
        · cases h
        · rename_i hrp
          split at h
          · rename_i t' ivs'
            cases h
            unfold rollbackProblem at hrp
            split at hrp
            · cases hrp
            · simp only at hrp
              split at hrp
              · cases hrp
              · split at hrp
                · cases hrp
                · rename_i h1 h2
                  simp only [Bool.or_eq_true, beq_iff_eq, not_or] at h1
                  exact ⟨h1.1, by omega⟩
          · cases h
      · cases h
  · dsimp only at h
    split at h <;> cases h

theorem decide_rollback_next {scope : Option AuditScope} {hooks : Hooks}
    {mk : Op → World → World → Entry} {ctx : Ctx} {replaying : Bool}
    {bAt : Nat → Option Boundary} {ops : List Op} {w w' : World} {es : List Entry}
    (hr : ops.any Op.isRollback = true)
    (h : decide scope hooks mk ctx replaying bAt ops w = .next w' es) : w' = w ∧ es = [] := by
  unfold decide at h
  simp only [hr, ↓reduceIte] at h
  split at h
  · cases h; exact ⟨rfl, rfl⟩
  · split at h
    · split at h
      · cases h; exact ⟨rfl, rfl⟩
      · split at h
        · cases h
        · cases h; exact ⟨rfl, rfl⟩
    · cases h; exact ⟨rfl, rfl⟩

theorem decide_replaying_rollback {scope : Option AuditScope} {hooks : Hooks}
    {mk : Op → World → World → Entry} {ctx : Ctx}
    {bAt : Nat → Option Boundary} {ops : List Op} {w : World}
    (hr : ops.any Op.isRollback = true) :
    decide scope hooks mk ctx true bAt ops w = .next w [] := by
  simp [decide, hr]

/-- Entries that agree once their branch and source are erased. -/
def MkAgree (mk1 mk2 : Op → World → World → Entry) : Prop :=
  ∀ op pre post, Entry.erase (mk1 op pre post) = Entry.erase (mk2 op pre post)

theorem applyOps_agree {mk1 mk2 : Op → World → World → Entry} (hmk : MkAgree mk1 mk2)
    (ctx : Ctx) {h1 h2 : Hooks} (hok : h1.applyOk = h2.applyOk) :
    ∀ (ops : List Op) (w : World),
      (applyOps mk1 ctx h1 ops w).1 = (applyOps mk2 ctx h2 ops w).1 ∧
      (applyOps mk1 ctx h1 ops w).2.map Entry.erase = (applyOps mk2 ctx h2 ops w).2.map Entry.erase
  | [], w => by simp [applyOps]
  | op :: rest, w => by
    simp only [applyOps, hok]
    split
    · exact applyOps_agree hmk ctx hok rest w
    · rename_i w' _
      have ih := applyOps_agree hmk ctx hok rest w'
      exact ⟨ih.1, by simp only [List.map_cons, hmk op w w', ih.2]⟩

/-- Two decisions on the same batch without a rollback, whose hooks reject alike and apply
alike, change the world alike and apply the same ops. -/
theorem decide_agree {scope : Option AuditScope} {h1 h2 : Hooks}
    {mk1 mk2 : Op → World → World → Entry} (hmk : MkAgree mk1 mk2) {ctx : Ctx}
    {r1 r2 : Bool} {b1 b2 : Nat → Option Boundary} {ops : List Op} {w : World}
    (hr : ops.any Op.isRollback = false)
    (hx : (h1.extra ctx ops w).isSome = (h2.extra ctx ops w).isSome)
    (hok : h1.applyOk = h2.applyOk) :
    ∃ w' es1 es2, decide scope h1 mk1 ctx r1 b1 ops w = .next w' es1 ∧
      decide scope h2 mk2 ctx r2 b2 ops w = .next w' es2 ∧
      es1.map Entry.erase = es2.map Entry.erase := by
  have ha := applyOps_agree hmk ctx hok
    (ops.filter (fun op => op.family == .message) ++ ops.filter (fun op => op.family != .message)) w
  simp only [decide, hr, Bool.false_eq_true, ↓reduceIte, hx]
  split
  · exact ⟨w, [], [], rfl, rfl, rfl⟩
  · exact ⟨_, _, _, rfl, by rw [ha.1], ha.2⟩

section
variable {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
  {replay : Option Replay}

theorem tick_next {c : Cfg} (hc : c.ended = none)
    (hn : (tick scope env policy b replay c).ended = none) :
    env.cut b c.rev c.shared c.st = none ∧ env.done b c.rev c.st = false ∧
    desynced env replay c.rev c.st = false ∧
    ∃ w es, tDecide scope env policy b replay c = .next w es ∧
      tick scope env policy b replay c =
        { rev := c.rev + 1,
          shared := env.notice c.shared b c.rev (tOps policy b replay c) es,
          st := env.step b c.rev (tOut env b replay c) { tSt b replay c with world := w },
          steps := c.steps ++ [tStep scope env policy b replay c (tOut env b replay c)],
          entries := c.entries ++ es, ended := none, rollback := none } := by
  rw [tick_body scope env policy b replay c hc] at hn ⊢
  split at hn
  · cases hn
  · rename_i hcut
    split at hn
    · cases hn
    · rename_i hdone
      split at hn
      · cases hn
      · rename_i hdes
        refine ⟨hcut, by simpa using hdone, by simpa using hdes, ?_⟩
        split at hn
        · cases hn
        · rename_i w es hd
          refine ⟨w, es, hd, ?_⟩
          simp only [hdone, hdes, Bool.false_eq_true, ↓reduceIte]

theorem tick_shape (c : Cfg) (hc : c.ended = none) :
    ((tick scope env policy b replay c).ended = none ∧
      (tick scope env policy b replay c).rev = c.rev + 1 ∧
      ∃ s, (tick scope env policy b replay c).steps = c.steps ++ [s]) ∨
    ((tick scope env policy b replay c).ended ≠ none ∧
      (tick scope env policy b replay c).rev = c.rev ∧
      ∃ l : List Step, l.length ≤ 1 ∧ (tick scope env policy b replay c).steps = c.steps ++ l) := by
  cases hn : (tick scope env policy b replay c).ended with
  | none =>
    obtain ⟨_, _, _, w, es, _, heq⟩ := tick_next hc hn
    left; rw [heq]; exact ⟨rfl, rfl, _, rfl⟩
  | some x =>
    right
    refine ⟨by simp, ?_⟩
    rw [tick_body scope env policy b replay c hc] at hn ⊢
    split
    · exact ⟨rfl, [], by simp, by simp⟩
    · split
      · exact ⟨rfl, [], by simp, by simp⟩
      · split
        · exact ⟨rfl, [], by simp, by simp⟩
        · split
          · exact ⟨rfl, [_], by simp, rfl⟩
          · simp_all

theorem run_succ : ∀ (n : Nat) (c : Cfg),
    run scope env policy b replay (n + 1) c = tick scope env policy b replay (run scope env policy b replay n c)
  | 0, _ => rfl
  | n + 1, c => by
    show run scope env policy b replay (n + 1) (tick scope env policy b replay c) = _
    rw [run_succ n]; rfl

theorem run_add : ∀ (m n : Nat) (c : Cfg),
    run scope env policy b replay (m + n) c = run scope env policy b replay n (run scope env policy b replay m c)
  | 0, n, c => by simp [run]
  | m + 1, n, c => by
    rw [Nat.add_right_comm]
    show run scope env policy b replay (m + n) (tick scope env policy b replay c) = _
    rw [run_add m n]; rfl

/-- How far a run from a running configuration has got. -/
theorem run_rev : ∀ (j : Nat) (c : Cfg), c.ended = none →
    ((run scope env policy b replay j c).ended = none →
      (run scope env policy b replay j c).rev = c.rev + j ∧
      (run scope env policy b replay j c).steps.length = c.steps.length + j) ∧
    ((run scope env policy b replay j c).ended ≠ none →
      (run scope env policy b replay j c).rev + 1 ≤ c.rev + j)
  | 0, c, hc => ⟨fun _ => by simp [run], fun h => absurd hc h⟩
  | j + 1, c, hc => by
    rw [run_succ]
    have ih := run_rev j c hc
    cases hj : (run scope env policy b replay j c).ended with
    | some x =>
      rw [tick_ended hj]
      exact ⟨fun h => (by rw [hj] at h; cases h), fun _ => (by have := ih.2 (by simp [hj]); omega)⟩
    | none =>
      have ⟨h1, h2⟩ := ih.1 hj
      rcases tick_shape _ hj with ⟨hn, hr, s, hs⟩ | ⟨hn, hr, l, _, hs⟩
      · exact ⟨fun _ => ⟨by omega, by simp [hs, h2]; omega⟩, fun h => absurd hn h⟩
      · exact ⟨fun h => absurd h hn, fun _ => by omega⟩

theorem run_steps_prefix : ∀ (m n : Nat) (c : Cfg),
    (run scope env policy b replay m c).steps <+: (run scope env policy b replay (m + n) c).steps
  | m, 0, c => List.prefix_refl _
  | m, n + 1, c => by
    rw [← Nat.add_assoc, run_succ]
    refine (run_steps_prefix m n c).trans ?_
    cases hj : (run scope env policy b replay (m + n) c).ended with
    | some x => rw [tick_ended hj]; exact List.prefix_refl _
    | none =>
      rcases tick_shape _ hj with ⟨_, _, s, hs⟩ | ⟨_, _, l, _, hs⟩ <;> rw [hs] <;>
        exact List.prefix_append _ _

/-- A recorded rollback targets a revision the branch reached. -/
def RollbackRev (c : Cfg) : Prop := ∀ t ivs, c.rollback = some (t, ivs) → t ≠ 0 ∧ t ≤ c.rev

theorem tick_rollbackRev {c : Cfg} (h : RollbackInv c) (h' : RollbackRev c) :
    RollbackRev (tick scope env policy b replay c) := by
  cases hc : c.ended with
  | some x => rw [tick_ended hc]; exact h'
  | none =>
    have hr : c.rollback = none := by
      cases hro : c.rollback with
      | none => rfl
      | some _ => have := h (by simp [hro]); rw [hc] at this; cases this
    intro t ivs ht
    have hb := tick_body scope env policy b replay c hc
    cases hcut : env.cut b c.rev c.shared c.st with
    | some x => simp only [hb, hcut, hr] at ht; cases ht
    | none =>
      cases hdone : env.done b c.rev c.st with
      | true => simp only [hb, hcut, hdone, hr, ↓reduceIte] at ht; cases ht
      | false =>
        cases hdes : desynced env replay c.rev c.st with
        | true => simp only [hb, hcut, hdone, hdes, hr, Bool.false_eq_true, ↓reduceIte] at ht; cases ht
        | false =>
          cases hd : tDecide scope env policy b replay c with
          | next w es => simp only [hb, hcut, hdone, hdes, hd, Bool.false_eq_true, ↓reduceIte] at ht; cases ht
          | rollback es t' ivs' =>
            simp only [hb, hcut, hdone, hdes, hd, Bool.false_eq_true, ↓reduceIte,
              Option.some.injEq, Prod.mk.injEq] at ht ⊢
            obtain ⟨rfl, rfl⟩ := ht
            have := decide_rollback hd
            simp only [tCtx] at this
            exact this

theorem run_rollbackRev : ∀ (n : Nat) (c : Cfg), RollbackInv c → RollbackRev c →
    RollbackRev (run scope env policy b replay n c)
  | 0, _, _, h => h
  | n + 1, _, h, h' => run_rollbackRev n _ (tick_rollbackInv h) (tick_rollbackRev h h')

end

end Scope.Branch
