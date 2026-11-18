import Scope.Proofs.BranchTick

/-! Replay fidelity (property 1). -/

namespace Scope.Branch

section
variable {scope : Option AuditScope} {env : Env} {policy : Policy} {p c : Nat}
  {rp : Option Replay} {R : Replay}

theorem child_ops {cc : Cfg} {s : Step} (hlt : cc.rev < R.fork)
    (hs : R.steps[cc.rev - 1]? = some s) :
    tOps policy c (some R) cc = s.decisions ∧ tOver env policy c (some R) cc = s.overBudget ∧
    tOut env c (some R) cc = s.output ∧ tSt c (some R) cc = cc.st ∧
    replayingAt (some R) cc.rev = true := by
  have hr : replayingAt (some R) cc.rev = true := by simp [replayingAt, hlt]
  have hf : forkAt (some R) cc.rev = false := by simp [forkAt]; omega
  refine ⟨?_, ?_, ?_, ?_, hr⟩
  · simp [tOps, batchAt, hlt, hs]
  · simp [tOver, hr, recordedAt, hs]
  · simp [tOut, hr, recordedAt, hs]
  · simp [tSt, hf]

theorem decide_nil {hooks : Hooks} {mk : Op → World → World → Entry} {ctx : Ctx} {rpl : Bool}
    {bAt : Nat → Option Boundary} {w : World} :
    decide scope hooks mk ctx rpl bAt [] w = .next w [] := by
  simp [decide, applyOps]

theorem decide_rejected {hooks : Hooks} {mk : Op → World → World → Entry} {ctx : Ctx}
    {rpl : Bool} {bAt : Nat → Option Boundary} {ops : List Op} {w : World}
    (hr : ops.any Op.isRollback = false) (hx : (hooks.extra ctx ops w).isSome = true) :
    decide scope hooks mk ctx rpl bAt ops w = .next w [] := by
  simp [decide, hr, hx]

/-- One replayed boundary: the child, at its parent's state and revision, does what its
parent did there, or is cut short. -/
theorem fidelity_step (h8 : T8 env) {pc cc : Cfg} (hpe : pc.ended = none)
    (hpn : (tick scope env policy p rp pc).ended = none) (hce : cc.ended = none)
    (hst : cc.st = pc.st) (hrev : cc.rev = pc.rev) (hlt : cc.rev < R.fork)
    (hpf : forkAt rp pc.rev = false)
    (hs : R.steps[cc.rev - 1]? = some (tStep scope env policy p rp pc (tOut env p rp pc))) :
    CutOrRunning (tick scope env policy c (some R) cc) ∧
    ((tick scope env policy c (some R) cc).ended = none →
      (tick scope env policy c (some R) cc).st = (tick scope env policy p rp pc).st ∧
      (tick scope env policy c (some R) cc).rev = (tick scope env policy p rp pc).rev ∧
      ∃ es1 es2, (tick scope env policy c (some R) cc).entries = cc.entries ++ es1 ∧
        (tick scope env policy p rp pc).entries = pc.entries ++ es2 ∧
        es1.map Entry.erase = es2.map Entry.erase) := by
  obtain ⟨_, hpdone, _, w, es, hpd, hpeq⟩ := tick_next hpe hpn
  obtain ⟨hops, hover, hout, htst, hrpl⟩ := child_ops (env := env) (policy := policy) (s := tStep scope env policy p rp pc
    (tOut env p rp pc)) hlt hs
  have hb := tick_body scope env policy c (some R) cc hce
  cases hcut : env.cut c cc.rev cc.shared cc.st with
  | some x =>
    rw [hb, hcut]
    exact ⟨Or.inr ⟨x, rfl⟩, fun h => nomatch h⟩
  | none =>
  have hdone : env.done c cc.rev cc.st = false := by rw [h8.2 c p, hst, hrev]; exact hpdone
  have hdes : desynced env (some R) cc.rev cc.st = false := by
    simp [desynced, Nat.le_of_lt hlt, hs, tStep, hst]
  have hctx : tCtx env cc = tCtx env pc := by simp [tCtx, hst, hrev]
  have hpst : tSt p rp pc = pc.st := by simp [tSt, hpf]
  have hmk : MkAgree (tMk scope env c (some R) cc) (tMk scope env p rp pc) := by
    intro op pre post; simp [tMk, Entry.erase, hctx, hrev]
  -- the child's batch is what the parent recorded
  have hcv : tVetoed scope env policy c (some R) cc = false := by simp [tVetoed, hrpl]
  -- the child's decision
  have key : ∃ es', tDecide scope env policy c (some R) cc = .next w es' ∧
      es'.map Entry.erase = es.map Entry.erase := by
    simp only [tStep] at hops hover hout
    by_cases hv : tVetoed scope env policy p rp pc = true
    · -- vetoed in the parent: it recorded nothing, applied nothing
      have hnr : (tOps policy p rp pc).any Op.isRollback = false := by
        simp only [tVetoed, Bool.and_eq_true, Bool.not_eq_true'] at hv; exact hv.1.1.1.1
      have hpx : ((tHooks scope env policy p rp pc).extra (tCtx env pc) (tOps policy p rp pc)
          (tSt p rp pc).world).isSome = true := by
        simp only [tHooks, hnr, Bool.false_eq_true, ↓reduceIte, hv, Bool.or_true]
        split <;> rfl
      have hpd' := hpd
      unfold tDecide at hpd'
      rw [decide_rejected hnr hpx] at hpd'
      cases hpd'
      refine ⟨[], ?_, rfl⟩
      unfold tDecide
      rw [hops]
      simp only [tRecorded, hv, ↓reduceIte, htst, hst, hpst]
      exact decide_nil
    · have hv' : tVetoed scope env policy p rp pc = false := by simpa using hv
      have hops' : tOps policy c (some R) cc = tOps policy p rp pc := by
        rw [hops]; simp [tRecorded, hv']
      cases hr : (tOps policy p rp pc).any Op.isRollback with
      | true =>
        have hpd' := hpd
        unfold tDecide at hpd'
        obtain ⟨rfl, rfl⟩ := decide_rollback_next hr hpd'
        refine ⟨[], ?_, rfl⟩
        unfold tDecide
        rw [hrpl, hops', htst, hst, ← hpst]
        exact decide_replaying_rollback hr
      | false =>
        have hx : ((tHooks scope env policy c (some R) cc).extra (tCtx env pc) (tOps policy p rp pc)
            (tSt p rp pc).world).isSome =
            ((tHooks scope env policy p rp pc).extra (tCtx env pc) (tOps policy p rp pc)
            (tSt p rp pc).world).isSome := by
          simp only [tHooks, hr, Bool.false_eq_true, ↓reduceIte, hcv, hv', hover]
        have hok : (tHooks scope env policy c (some R) cc).applyOk =
            (tHooks scope env policy p rp pc).applyOk := by simp [tHooks, hout]
        obtain ⟨w', es1, es2, h1, h2, h3⟩ := decide_agree (scope := scope)
          (r1 := replayingAt (some R) cc.rev) (r2 := replayingAt rp pc.rev)
          (b1 := tBAt env cc) (b2 := tBAt env pc) hmk hr hx hok
        have hpd' := hpd
        unfold tDecide at hpd'
        rw [hpd'] at h2
        cases h2
        refine ⟨es1, ?_, h3⟩
        unfold tDecide
        rw [hops', hctx, htst, hst, ← hpst]
        exact h1
  obtain ⟨es', hcd, herase⟩ := key
  have hceq : tick scope env policy c (some R) cc =
      { rev := cc.rev + 1,
        shared := env.notice cc.shared c cc.rev (tOps policy c (some R) cc) es',
        st := env.step c cc.rev (tOut env c (some R) cc) { tSt c (some R) cc with world := w },
        steps := cc.steps ++ [tStep scope env policy c (some R) cc (tOut env c (some R) cc)],
        entries := cc.entries ++ es', ended := none, rollback := none } := by
    rw [hb, hcut]
    simp only [hdone, hdes, Bool.false_eq_true, ↓reduceIte, hcd]
  rw [hceq, hpeq]
  refine ⟨Or.inl rfl, fun _ => ⟨?_, by simp [hrev], es', es, rfl, rfl, herase⟩⟩
  simp only
  rw [h8.1 c p, hout, htst, hst, hrev, ← hpst]
  rfl

end

section
variable {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
  {replay : Option Replay}

theorem branch_steps (fuel : Nat) (sh : Shared) (s0 : State) :
    (branch scope env policy b replay fuel sh s0).steps =
      (run scope env policy b replay fuel (start env sh b s0)).steps ∧
    (branch scope env policy b replay fuel sh s0).rollback =
      (run scope env policy b replay fuel (start env sh b s0)).rollback := by
  unfold branch; simp only; split <;> exact ⟨rfl, rfl⟩

theorem start_inv (sh : Shared) (s0 : State) :
    RollbackInv (start env sh b s0) ∧ RollbackRev (start env sh b s0) := by
  refine ⟨fun h => ?_, fun _ _ h => ?_⟩ <;> simp [start] at h

/-- A branch that rolled back to `t` was running at every boundary before `t`. -/
theorem running_before_rollback {fuel : Nat} {sh : Shared} {s0 : State} {t : Nat}
    {ivs : List Op} (hF : (branch scope env policy b replay fuel sh s0).rollback = some (t, ivs)) :
    t ≠ 0 ∧ t ≤ fuel ∧ ∀ i, i < t →
      (run scope env policy b replay i (start env sh b s0)).ended = none := by
  rw [(branch_steps fuel sh s0).2] at hF
  have hi := start_inv (env := env) (b := b) sh s0
  have hinv := run_rollbackInv (scope := scope) (env := env) (policy := policy) (b := b) (replay := replay) fuel _ hi.1
  have hrev := run_rollbackRev (scope := scope) (env := env) (policy := policy) (b := b) (replay := replay) fuel _ hi.1 hi.2
  have hend := hinv (by simp [hF])
  have ⟨ht0, htr⟩ := hrev t ivs hF
  have hr := (run_rev (scope := scope) (env := env) (policy := policy) (b := b) (replay := replay) fuel
    (start env sh b s0) (by simp [start])).2 (by simp [hend])
  rw [show (start env sh b s0).rev = 1 from rfl] at hr
  refine ⟨ht0, by omega, fun i hi' => ?_⟩
  cases hx : (run scope env policy b replay i (start env sh b s0)).ended with
  | none => rfl
  | some x =>
    exfalso
    have hri := (run_rev (scope := scope) (env := env) (policy := policy) (b := b) (replay := replay) i
      (start env sh b s0) (by simp [start])).2 (by simp [hx])
    rw [show (start env sh b s0).rev = 1 from rfl] at hri
    have heq : run scope env policy b replay fuel (start env sh b s0) =
        run scope env policy b replay i (start env sh b s0) := by
      have : fuel = i + (fuel - i) := by omega
      rw [this, run_add, run_ended _ hx]
    rw [heq] at htr
    omega

theorem getElem?_of_prefix {α : Type} {l₁ l₂ : List α} (h : l₁ <+: l₂) {i : Nat}
    (hi : i < l₁.length) : l₂[i]? = l₁[i]? := by
  obtain ⟨l, rfl⟩ := h
  exact List.getElem?_append_left hi

end

theorem fidelity : Fidelity := by
  intro scope env policy p c rp fuel sh sh' s0 R h8 hnest hR
  -- the parent's rollback and what the child replays
  have hsteps := branch_steps (scope := scope) (env := env) (policy := policy) (b := p)
    (replay := rp) fuel sh s0
  cases hF : (branch scope env policy p rp fuel sh s0).rollback with
  | none => simp [childReplay, hF] at hR
  | some tv =>
  obtain ⟨t, ivs⟩ := tv
  simp only [childReplay, hF, Option.map_some, Option.some.injEq] at hR
  subst hR
  obtain ⟨ht0, htf, hrun⟩ := running_before_rollback hF
  simp only
  let R0 : Replay :=
    { steps := (branch scope env policy p rp fuel sh s0).steps.take t, fork := t, interventions := ivs }
  -- the parent's recorded step at each replayed boundary
  have hrec : ∀ i, i + 1 < t →
      ((R0 : Replay).steps)[i]? =
        some (tStep scope env policy p rp (run scope env policy p rp i (start env sh p s0))
          (tOut env p rp (run scope env policy p rp i (start env sh p s0)))) := by
    intro i hi
    have hn := hrun (i + 1) hi
    rw [run_succ] at hn
    obtain ⟨_, _, _, w, es, _, heq⟩ := tick_next (hrun i (by omega)) hn
    have hpre := run_steps_prefix (scope := scope) (env := env) (policy := policy) (b := p)
      (replay := rp) (i + 1) (fuel - (i + 1)) (start env sh p s0)
    rw [Nat.add_sub_cancel' (by omega), run_succ, heq] at hpre
    have hlen := ((run_rev (scope := scope) (env := env) (policy := policy) (b := p) (replay := rp)
      i (start env sh p s0) (by simp [start])).1 (hrun i (by omega))).2
    rw [show (start env sh p s0).steps.length = 0 from rfl, Nat.zero_add] at hlen
    show ((branch scope env policy p rp fuel sh s0).steps.take t)[i]? = _
    rw [List.getElem?_take_of_lt (by omega), hsteps.1]
    rw [getElem?_of_prefix hpre (by simp [hlen])]
    simp [hlen]
  -- induction over the replayed boundaries
  have main : ∀ i, i < t →
      CutOrRunning (run scope env policy c
        (some R0) i (start env sh' c s0)) ∧
      ((run scope env policy c (some R0) i (start env sh' c s0)).ended = none →
        (run scope env policy c (some R0) i (start env sh' c s0)).st = (run scope env policy p rp i (start env sh p s0)).st ∧
        (run scope env policy c (some R0) i (start env sh' c s0)).rev = (run scope env policy p rp i (start env sh p s0)).rev ∧
        (run scope env policy c (some R0) i (start env sh' c s0)).entries.map Entry.erase =
          (run scope env policy p rp i (start env sh p s0)).entries.map Entry.erase) := by
    intro i
    induction i with
    | zero => intro _; exact ⟨Or.inl rfl, fun _ => ⟨rfl, rfl, rfl⟩⟩
    | succ i ih =>
      intro hi
      obtain ⟨hcr, heqs⟩ := ih (by omega)
      rw [run_succ, run_succ]
      cases hce : (run scope env policy c (some R0) i (start env sh' c s0)).ended with
      | some x =>
        rw [tick_ended hce]
        refine ⟨hcr, fun h => ?_⟩
        rw [hce] at h; cases h
      | none =>
        obtain ⟨hst, hrv, hes⟩ := heqs hce
        have hpe := hrun i (by omega)
        have hpn : (tick scope env policy p rp (run scope env policy p rp i (start env sh p s0))).ended = none := by
          rw [← run_succ]; exact hrun (i + 1) hi
        have hprev := ((run_rev (scope := scope) (env := env) (policy := policy) (b := p) (replay := rp)
          i (start env sh p s0) (by simp [start])).1 hpe).1
        rw [show (start env sh p s0).rev = 1 from rfl] at hprev
        have hpf : forkAt rp (run scope env policy p rp i (start env sh p s0)).rev = false := by
          cases rp with
          | none => rfl
          | some r' =>
            have := hnest r' rfl
            simp only [forkAt, beq_eq_false_iff_ne]
            simp only at this
            omega
        have hlt : (run scope env policy c (some R0) i (start env sh' c s0)).rev < R0.fork := by
          show _ < t; rw [hrv]; omega
        have hs : R0.steps[(run scope env policy c (some R0) i (start env sh' c s0)).rev - 1]? =
            some (tStep scope env policy p rp (run scope env policy p rp i (start env sh p s0))
              (tOut env p rp (run scope env policy p rp i (start env sh p s0)))) := by
          rw [show (run scope env policy c (some R0) i (start env sh' c s0)).rev - 1 = i by
            rw [hrv]; omega]
          exact hrec i hi
        obtain ⟨h1, h2⟩ := fidelity_step (R := R0) h8 hpe hpn hce hst hrv hlt hpf hs
        refine ⟨h1, fun hn => ?_⟩
        obtain ⟨hst', hrv', es1, es2, he1, he2, he⟩ := h2 hn
        refine ⟨hst', hrv', ?_⟩
        rw [he1, he2, List.map_append, List.map_append, hes, he]
  intro i hi
  exact main i hi

end Scope.Branch
