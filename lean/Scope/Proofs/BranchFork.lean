import Scope.Proofs.BranchFidelity

/-! The fork boundary, the run-level fail-safe and the ids (properties 1, 2c, 4, 6). -/

namespace Scope.Branch

section
variable {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
  {replay : Option Replay}

/-- A boundary that reached the decision: what passed, and the step it recorded. -/
theorem tick_decided {c : Cfg} (hc : c.ended = none)
    (h : (tick scope env policy b replay c).ended = none ∨
      (tick scope env policy b replay c).ended = some .rolledBack) :
    env.cut b c.rev c.shared c.st = none ∧ env.done b c.rev c.st = false ∧
    desynced env replay c.rev c.st = false ∧
    ∃ out, (tick scope env policy b replay c).steps =
      c.steps ++ [tStep scope env policy b replay c out] := by
  have hb := tick_body scope env policy b replay c hc
  cases hcut : env.cut b c.rev c.shared c.st with
  | some x =>
    rw [hb, hcut] at h; cases x <;> simp [Cut.status] at h
  | none =>
  cases hdone : env.done b c.rev c.st with
  | true =>
    rw [hb, hcut] at h; simp only [hdone, ↓reduceIte] at h
    unfold finished at h; split at h <;> (try split at h) <;> simp at h
  | false =>
  cases hdes : desynced env replay c.rev c.st with
  | true =>
    rw [hb, hcut] at h; simp [hdone, hdes] at h
  | false =>
  refine ⟨rfl, rfl, rfl, ?_⟩
  rw [hb, hcut]
  simp only [hdone, hdes, Bool.false_eq_true, ↓reduceIte]
  split
  · exact ⟨0, rfl⟩
  · exact ⟨_, rfl⟩

/-- Past the checks, a boundary either continues or rolls back. -/
theorem tick_after_checks {c : Cfg} (hc : c.ended = none)
    (hcut : env.cut b c.rev c.shared c.st = none) (hdone : env.done b c.rev c.st = false)
    (hdes : desynced env replay c.rev c.st = false) :
    (tick scope env policy b replay c).ended = none ∨
      (tick scope env policy b replay c).ended = some .rolledBack := by
  rw [tick_body scope env policy b replay c hc, hcut]
  simp only [hdone, hdes, Bool.false_eq_true, ↓reduceIte]
  split
  · exact Or.inr rfl
  · exact Or.inl rfl

theorem run_rev_mono : ∀ (n : Nat) (c : Cfg), c.rev ≤ (run scope env policy b replay n c).rev
  | 0, _ => Nat.le_refl _
  | n + 1, c => by
    rw [run_succ]
    have ih := run_rev_mono n c
    cases hj : (run scope env policy b replay n c).ended with
    | some x => rw [tick_ended hj]; exact ih
    | none =>
      rcases tick_shape (scope := scope) (env := env) (policy := policy) (b := b) (replay := replay) _ hj with ⟨_, hr, _⟩ | ⟨_, hr, _⟩ <;> omega

/-- A run that got past boundary `k` was running there. -/
theorem running_of_rev_gt {sh : Shared} {s0 : State} {n k : Nat}
    (h : k < (run scope env policy b replay n (start env sh b s0)).rev) :
    ∀ j, j ≤ k → (run scope env policy b replay j (start env sh b s0)).ended = none := by
  intro j hj
  cases hx : (run scope env policy b replay j (start env sh b s0)).ended with
  | none => rfl
  | some x =>
    exfalso
    have hrj := (run_rev (scope := scope) (env := env) (policy := policy) (b := b) (replay := replay)
      j (start env sh b s0) (by simp [start])).2 (by simp [hx])
    rw [show (start env sh b s0).rev = 1 from rfl] at hrj
    by_cases hn : j ≤ n
    · have heq : run scope env policy b replay n (start env sh b s0) =
          run scope env policy b replay j (start env sh b s0) := by
        have : n = j + (n - j) := by omega
        rw [this, run_add, run_ended _ hx]
      rw [heq] at h; omega
    · have hrn := run_rev (scope := scope) (env := env) (policy := policy) (b := b) (replay := replay)
        n (start env sh b s0) (by simp [start])
      rw [show (start env sh b s0).rev = 1 from rfl] at hrn
      cases hy : (run scope env policy b replay n (start env sh b s0)).ended with
      | none => have := (hrn.1 hy).1; omega
      | some y => have := hrn.2 (by simp [hy]); omega

end

theorem failSafeRun : FailSafeRun := by
  intro scope env policy c R n sh s0 k hk1 hkf hk
  have hrun := running_of_rev_gt hk
  have h1 := hrun (k - 1) (by omega)
  have h2 := hrun k (Nat.le_refl _)
  rw [show k = (k - 1) + 1 by omega, run_succ] at h2
  obtain ⟨_, _, hdes, _⟩ := tick_next h1 h2
  have hrev := ((run_rev (scope := scope) (env := env) (policy := policy) (b := c) (replay := some R)
    (k - 1) (start env sh c s0) (by simp [start])).1 h1).1
  rw [show (start env sh c s0).rev = 1 from rfl, show 1 + (k - 1) = k by omega] at hrev
  rw [hrev] at hdes
  unfold desynced at hdes
  simp only [hkf, ↓reduceIte] at hdes
  cases hs : R.steps[k - 1]? with
  | none => rw [hs] at hdes; cases hdes
  | some st => rw [hs] at hdes; simpa using hdes

/-- The parent's boundary at its rollback target `t` reached the decision, and recorded the
step the child replays at its fork. -/
theorem parent_at_target {scope : Option AuditScope} {env : Env} {policy : Policy} {p : Nat}
    {rp : Option Replay} {fuel : Nat} {sh : Shared} {s0 : State} {t : Nat} {ivs : List Op}
    (hF : (branch scope env policy p rp fuel sh s0).rollback = some (t, ivs)) :
    let pc := run scope env policy p rp (t - 1) (start env sh p s0)
    pc.ended = none ∧ pc.rev = t ∧
    env.cut p pc.rev pc.shared pc.st = none ∧ env.done p pc.rev pc.st = false ∧
    ∃ out, ((branch scope env policy p rp fuel sh s0).steps.take t)[t - 1]? =
      some (tStep scope env policy p rp pc out) := by
  obtain ⟨ht0, htf, hrun⟩ := running_before_rollback hF
  have hsteps := branch_steps (scope := scope) (env := env) (policy := policy) (b := p)
    (replay := rp) fuel sh s0
  have hpe := hrun (t - 1) (by omega)
  have hpr := (run_rev (scope := scope) (env := env) (policy := policy) (b := p) (replay := rp)
    (t - 1) (start env sh p s0) (by simp [start])).1 hpe
  rw [show (start env sh p s0).rev = 1 from rfl, show (start env sh p s0).steps.length = 0 from rfl,
    show 1 + (t - 1) = t by omega, Nat.zero_add] at hpr
  -- the parent's boundary `t` continued or rolled back
  have hdec : (tick scope env policy p rp (run scope env policy p rp (t - 1) (start env sh p s0))).ended = none ∨
      (tick scope env policy p rp (run scope env policy p rp (t - 1) (start env sh p s0))).ended =
        some .rolledBack := by
    rw [← run_succ, show t - 1 + 1 = t by omega]
    cases hy : (run scope env policy p rp t (start env sh p s0)).ended with
    | none => exact Or.inl rfl
    | some y =>
      right
      have hG : run scope env policy p rp fuel (start env sh p s0) =
          run scope env policy p rp t (start env sh p s0) := by
        rw [show fuel = t + (fuel - t) by omega, run_add, run_ended _ hy]
      have hinv := run_rollbackInv (scope := scope) (env := env) (policy := policy) (b := p)
        (replay := rp) fuel _ (start_inv (env := env) (b := p) sh s0).1
      have hrb : (run scope env policy p rp fuel (start env sh p s0)).rollback = some (t, ivs) :=
        hsteps.2 ▸ hF
      have := hinv (by rw [hrb]; simp)
      rw [hG, hy] at this
      exact this
  obtain ⟨hcut, hdone, _, out, hst⟩ := tick_decided hpe hdec
  refine ⟨hpe, hpr.1, hcut, hdone, out, ?_⟩
  have hpre := run_steps_prefix (scope := scope) (env := env) (policy := policy) (b := p)
    (replay := rp) t (fuel - t) (start env sh p s0)
  rw [Nat.add_sub_cancel' htf, show t = (t - 1) + 1 by omega, run_succ, hst] at hpre
  rw [List.getElem?_take_of_lt (by omega), hsteps.1, show t = (t - 1) + 1 by omega,
    getElem?_of_prefix hpre (by simp [hpr.2])]
  simp [hpr.2]

theorem throughFork : ThroughFork := by
  intro scope env policy p c rp fuel sh sh' s0 R h8 hnest hR
  have hfid := fidelity scope env policy p c rp fuel sh sh' s0 R h8 hnest hR
  cases hF : (branch scope env policy p rp fuel sh s0).rollback with
  | none => simp [childReplay, hF] at hR
  | some tv =>
  obtain ⟨t, ivs⟩ := tv
  simp only [childReplay, hF, Option.map_some, Option.some.injEq] at hR
  have hRf : R.fork = t := by rw [← hR]
  have hRs : R.steps = (branch scope env policy p rp fuel sh s0).steps.take t := by rw [← hR]
  obtain ⟨ht0, _, _⟩ := running_before_rollback hF
  obtain ⟨hpe, hprev, hpcut, hpdone, out, hrec⟩ := parent_at_target hF
  obtain ⟨hcr, heqs⟩ := hfid (t - 1) (by omega)
  show CutOrRunning (run scope env policy c (some R) R.fork (start env sh' c s0)) ∨
    (run scope env policy c (some R) R.fork (start env sh' c s0)).ended = some .rolledBack
  rw [hRf, show t = (t - 1) + 1 by omega, run_succ]
  cases hx : (run scope env policy c (some R) (t - 1) (start env sh' c s0)).ended with
  | some y =>
    rw [tick_ended hx]
    exact Or.inl hcr
  | none =>
    obtain ⟨hst, hrv, _⟩ := heqs hx
    have hb := tick_body scope env policy c (some R) _ hx
    cases hcut : env.cut c (run scope env policy c (some R) (t - 1) (start env sh' c s0)).rev
        (run scope env policy c (some R) (t - 1) (start env sh' c s0)).shared
        (run scope env policy c (some R) (t - 1) (start env sh' c s0)).st with
    | some z => rw [hb, hcut]; exact Or.inl (Or.inr ⟨z, rfl⟩)
    | none =>
      have hdone : env.done c (run scope env policy c (some R) (t - 1) (start env sh' c s0)).rev
          (run scope env policy c (some R) (t - 1) (start env sh' c s0)).st = false := by
        rw [h8.2 c p, hst, hrv]; exact hpdone
      have hdes : desynced env (some R) (run scope env policy c (some R) (t - 1) (start env sh' c s0)).rev
          (run scope env policy c (some R) (t - 1) (start env sh' c s0)).st = false := by
        rw [hrv, hprev, hst]
        simp only [desynced, hRf, Nat.le_refl, ↓reduceIte, hRs, hrec, tStep]
        simp
      rcases tick_after_checks (scope := scope) (policy := policy) hx hcut hdone hdes with h | h
      · exact Or.inl (Or.inl h)
      · exact Or.inr h

theorem decide_rollback_chain {scope : Option AuditScope} {hooks : Hooks}
    {mk : Op → World → World → Entry} (hmk : MkOk mk) {ctx : Ctx} {replaying : Bool}
    {bAt : Nat → Option Boundary} {ops : List Op} {w : World} {es : List Entry} {t : Nat}
    {ivs : List Op} (h : decide scope hooks mk ctx replaying bAt ops w = .rollback es t ivs) :
    Chain w es w := by
  unfold decide at h
  split at h
  · split at h
    · cases h
    · split at h
      · split at h
        · cases h
        · split at h
          · cases h
            exact ⟨hmk.pre .., by rw [hmk.post]; rfl⟩
          · cases h
      · cases h
  · dsimp only at h
    split at h <;> cases h

theorem tSt_world {b : Nat} {replay : Option Replay} {c : Cfg} : (tSt b replay c).world = c.st.world := by
  unfold tSt; split <;> rfl

/-- A boundary appends ops that passed the checks, chained from the branch's world. -/
theorem tick_new {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {replay : Option Replay} {c : Cfg} (hc : c.ended = none) :
    ∃ es w', (tick scope env policy b replay c).entries = c.entries ++ es ∧
      (∀ e ∈ es, Good scope e) ∧ Chain c.st.world es w' := by
  have hb := tick_body scope env policy b replay c hc
  have hmk : MkOk (tMk scope env b replay c) := ⟨fun _ _ _ => rfl, fun _ _ _ => rfl, fun _ _ _ => rfl⟩
  cases hcut : env.cut b c.rev c.shared c.st with
  | some x => rw [hb, hcut]; exact ⟨[], _, by simp, by simp, rfl⟩
  | none =>
  cases hdone : env.done b c.rev c.st with
  | true => rw [hb, hcut]; simp only [hdone, ↓reduceIte]; exact ⟨[], _, by simp, by simp, rfl⟩
  | false =>
  cases hdes : desynced env replay c.rev c.st with
  | true => rw [hb, hcut]; simp only [hdone, hdes, Bool.false_eq_true, ↓reduceIte]
            exact ⟨[], _, by simp, by simp, rfl⟩
  | false =>
  rw [hb, hcut]
  simp only [hdone, hdes, Bool.false_eq_true, ↓reduceIte]
  cases hd : tDecide scope env policy b replay c with
  | rollback es t ivs =>
    refine ⟨es, c.st.world, rfl, fun e he => decide_good' hd (by simpa [Outcome.entries] using he)
      hmk (fun _ _ _ => rfl) (fun _ _ _ => rfl), ?_⟩
    have := decide_rollback_chain hmk hd
    rwa [tSt_world] at this
  | next w es =>
    refine ⟨es, w, rfl, fun e he => decide_good' hd (by simpa [Outcome.entries] using he)
      hmk (fun _ _ _ => rfl) (fun _ _ _ => rfl), ?_⟩
    have := onlyApplied scope (tHooks scope env policy b replay c) (tMk scope env b replay c)
      (tCtx env c) (replayingAt replay c.rev) (tBAt env c) (tOps policy b replay c)
      (tSt b replay c).world w es (fun _ _ _ => ⟨rfl, rfl, rfl⟩) hd
    rwa [tSt_world] at this

theorem good_preserves_wf {s : AuditScope} {e : Entry} (h : Good (some s) e) (hw : e.pre.WF) :
    e.post.WF := by
  rcases h.2.2 with heq | ⟨ok, happ, _⟩
  · rw [heq]; exact hw
  · unfold World.WF; rw [(applyRaw_preserves happ).1]; exact hw

theorem good_view {s : AuditScope} {e : Entry} (h : Good (some s) e) (hw : e.pre.WF)
    (hc : carvedOut e.op = false) {a : Agent} (ha : s.allowsAgent a = false) :
    view e.post a = view e.pre a := by
  obtain ⟨_, hp, hpost⟩ := h
  rcases hpost with heq | ⟨ok, happ, hck⟩
  · rw [heq]
  · have hnot : a ∉ e.op.affected e.pre := fun hin => by
      have := (scopeSound s e.pre e.op (hp s rfl)).2.1 a hin
      rw [ha] at this; cases this
    exact frame e.ctx ok e.op e.pre e.post a hw hck hc happ hnot

theorem chain_view {s : AuditScope} : ∀ (es : List Entry) (w w' : World), Chain w es w' → w.WF →
    (∀ e ∈ es, Good (some s) e ∧ carvedOut e.op = false) →
    ∀ a, s.allowsAgent a = false → view w' a = view w a
  | [], w, w', h, _, _, _, _ => by simp only [Chain] at h; rw [h]
  | e :: rest, w, w', h, hw, hall, a, ha => by
    obtain ⟨hpre, hrest⟩ := h
    have he := hall e (List.mem_cons_self ..)
    subst hpre
    rw [chain_view rest e.post w' hrest (good_preserves_wf he.1 hw)
      (fun e' he' => hall e' (List.mem_cons_of_mem _ he')) a ha]
    exact good_view he.1 hw he.2 ha

theorem forkEffect : ForkEffect := by
  intro s env policy p c rp fuel sh sh' s0 R h8 hnest hR
  have hfid := fidelity (some s) env policy p c rp fuel sh sh' s0 R h8 hnest hR
  cases hF : (branch (some s) env policy p rp fuel sh s0).rollback with
  | none => simp [childReplay, hF] at hR
  | some tv =>
  obtain ⟨t, ivs⟩ := tv
  simp only [childReplay, hF, Option.map_some, Option.some.injEq] at hR
  have hRf : R.fork = t := by rw [← hR]
  obtain ⟨ht0, _, _⟩ := running_before_rollback hF
  dsimp only
  intro hce
  obtain ⟨_, heqs⟩ := hfid (R.fork - 1) (by omega)
  have hst := (heqs hce).1
  refine ⟨by rw [hst], fun hwf hcarve => ?_⟩
  obtain ⟨es, w', hent, hgood, hch⟩ := tick_new (scope := some s) (env := env) (policy := policy)
    (b := c) (replay := some R) hce
  have hdrop : (tick (some s) env policy c (some R)
      (run (some s) env policy c (some R) (R.fork - 1) (start env sh' c s0))).entries.drop
      (run (some s) env policy c (some R) (R.fork - 1) (start env sh' c s0)).entries.length = es := by
    rw [hent]; simp
  rw [hdrop] at hcarve ⊢
  refine ⟨w', hch, fun a ha => ?_⟩
  rw [chain_view es _ w' hch (by rw [hst]; exact hwf)
    (fun e he => ⟨hgood e he, hcarve e he⟩) a ha, hst]

theorem tSt_tag_ne {c : Nat} {R : Replay} {cc : Cfg} (h : cc.rev ≠ R.fork) :
    (tSt c (some R) cc).ids.tag = cc.st.ids.tag := by
  unfold tSt forkAt; simp [h]

theorem tSt_tag_eq {c : Nat} {R : Replay} {cc : Cfg} (h : cc.rev = R.fork) :
    (tSt c (some R) cc).ids.tag = c := by
  unfold tSt forkAt; simp [h, Ids.diverge]

/-- The child's id tag, by where it is relative to its fork. -/
def TagInv (c : Nat) (R : Replay) (cc : Cfg) : Prop :=
  (cc.rev < R.fork → cc.st.ids.tag = 0) ∧ (R.fork < cc.rev → cc.st.ids.tag = c) ∧
  (cc.rev = R.fork → cc.ended = none → cc.st.ids.tag = 0)

theorem tick_tagInv {scope : Option AuditScope} {env : Env} {policy : Policy} {c : Nat}
    {R : Replay} {cc : Cfg} (hk : TagKept env) (h : TagInv c R cc) :
    TagInv c R (tick scope env policy c (some R) cc) := by
  cases hce : cc.ended with
  | some x => rw [tick_ended hce]; exact h
  | none =>
  obtain ⟨hA, hB, hC⟩ := h
  have hb := tick_body scope env policy c (some R) cc hce
  cases hcut : env.cut c cc.rev cc.shared cc.st with
  | some x => rw [hb, hcut]; exact ⟨hA, hB, fun _ h' => nomatch h'⟩
  | none =>
  cases hdone : env.done c cc.rev cc.st with
  | true => rw [hb, hcut]; simp only [hdone, ↓reduceIte]; exact ⟨hA, hB, fun _ h' => nomatch h'⟩
  | false =>
  cases hdes : desynced env (some R) cc.rev cc.st with
  | true => rw [hb, hcut]; simp only [hdone, hdes, Bool.false_eq_true, ↓reduceIte]
            exact ⟨hA, hB, fun _ h' => nomatch h'⟩
  | false =>
  rw [hb, hcut]
  simp only [hdone, hdes, Bool.false_eq_true, ↓reduceIte]
  split
  · refine ⟨fun hl => ?_, fun hl => ?_, fun _ h' => nomatch h'⟩
    · dsimp only at hl ⊢; rw [tSt_tag_ne (by omega)]; exact hA hl
    · dsimp only at hl ⊢; rw [tSt_tag_ne (by omega)]; exact hB hl
  · refine ⟨fun hl => ?_, fun hl => ?_, fun hl _ => ?_⟩
    · dsimp only at hl ⊢; rw [hk, tSt_tag_ne (by omega)]; exact hA (by omega)
    · dsimp only at hl ⊢
      rw [hk]
      by_cases he : cc.rev = R.fork
      · rw [tSt_tag_eq he]
      · rw [tSt_tag_ne he]; exact hB (by omega)
    · dsimp only at hl ⊢; rw [hk, tSt_tag_ne (by omega)]; exact hA (by omega)

theorem idsOwnAfterFork : IdsOwnAfterFork := by
  intro scope env policy c R n sh s0 hk h1
  have : ∀ m (cc : Cfg), TagInv c R cc → TagInv c R (run scope env policy c (some R) m cc) := by
    intro m
    induction m with
    | zero => intro cc h; exact h
    | succ m ih => intro cc h; exact ih _ (tick_tagInv hk h)
  have h0 : TagInv c R (start env sh c s0) :=
    ⟨fun _ => rfl, fun h' => by simp [start] at h'; omega, fun _ _ => rfl⟩
  obtain ⟨hA, hB, _⟩ := this n _ h0
  exact ⟨hA, hB⟩

end Scope.Branch
