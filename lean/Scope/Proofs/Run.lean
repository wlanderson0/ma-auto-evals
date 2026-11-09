import Scope.Proofs.Check

namespace Scope

theorem touchOwn_preserves {ctx : Ctx} {op : Op} {w w' : World}
    (h : touchOwn ctx op w = .ok w') :
    w'.messages = w.messages ∧ w'.agents = w.agents := by
  unfold touchOwn at h
  split at h
  · cases h
  · cases h; exact ⟨rfl, rfl⟩

theorem applyRaw_preserves {ctx : Ctx} {ok : Bool} {op : Op} {w w' : World}
    (h : applyRaw ctx ok op w = .ok w') :
    w'.messages = w.messages ∧ w'.agents = w.agents := by
  cases op <;> simp only [applyRaw] at h
  all_goals first
    | exact touchOwn_preserves h
    | (repeat' split at h) <;> first | exact touchOwn_preserves h | (cases h <;> exact ⟨rfl, rfl⟩) | simp_all

theorem affected_congr {w w' : World} (h : w.messages = w'.messages) (op : Op) :
    op.affected w = op.affected w' := by
  cases op <;> simp [Op.affected, World.findMsg, h]

theorem ownProblem_congr {w w' : World} (h : w.messages = w'.messages) (s : AuditScope)
    (op : Op) : s.ownProblem w op = s.ownProblem w' op := by
  simp [AuditScope.ownProblem, affected_congr h]

theorem problem_congr {w w' : World} (h : w.messages = w'.messages) (s : AuditScope) :
    ∀ op, s.problem w op = none → s.problem w' op = none
  | op, hp => by
    rw [AuditScope.problem_none] at hp ⊢
    refine ⟨(ownProblem_congr h s op) ▸ hp.1, fun iv hiv => problem_congr h s iv (hp.2 iv hiv)⟩
termination_by op => sizeOf op
decreasing_by exact sizeOf_lt_of_intervention hiv

theorem checkProblem_some {s : AuditScope} {ctx : Ctx} {op : Op} {w : World}
    (h : checkProblem (some s) ctx op w = none) :
    s.problem w op = none ∧ checkProblem none ctx op w = none := by
  unfold checkProblem at h ⊢
  cases he : (op.agentField? == some "")
  · simp only [he, Bool.false_eq_true, ↓reduceIte] at h ⊢
    cases hp : s.problem w op with
    | some p => simp [hp] at h
    | none => simp only [Option.bind, hp] at h; exact ⟨rfl, h⟩
  · simp [he] at h

theorem checkProblem_congr {w w' : World} (h : w.agents = w'.agents) (ctx : Ctx) (op : Op) :
    checkProblem none ctx op w = checkProblem none ctx op w' := by
  simp [checkProblem, h]

/-- Everything a run's log entry needs: what the controller checked, and how the op changed
the world. -/
def Good (scope : Option AuditScope) (e : Entry) : Prop :=
  e.scope = scope ∧ (∀ s, scope = some s → s.problem e.pre e.op = none) ∧
    (e.post = e.pre ∨
      ∃ ok, applyRaw e.ctx ok e.op e.pre = .ok e.post ∧ checkProblem none e.ctx e.op e.pre = none)

structure MkOk (mk : Op → World → World → Entry) : Prop where
  op : ∀ op pre post, (mk op pre post).op = op
  pre : ∀ op pre post, (mk op pre post).pre = pre
  post : ∀ op pre post, (mk op pre post).post = post

theorem applyOps_chain {mk : Op → World → World → Entry} (hmk : MkOk mk) (ctx : Ctx)
    (hooks : Hooks) : ∀ (ops : List Op) (w : World),
      Chain w (applyOps mk ctx hooks ops w).2 (applyOps mk ctx hooks ops w).1
  | [], w => by simp [applyOps, Chain]
  | op :: rest, w => by
    simp only [applyOps]
    split
    · exact applyOps_chain hmk ctx hooks rest w
    · rename_i w' _
      refine ⟨hmk.pre .., ?_⟩
      rw [hmk.post]
      exact applyOps_chain hmk ctx hooks rest w'

theorem applyOps_entries {mk : Op → World → World → Entry} (hmk : MkOk mk) (ctx : Ctx)
    (hooks : Hooks) : ∀ (ops : List Op) (w : World),
      ∀ e ∈ (applyOps mk ctx hooks ops w).2, ∃ op ∈ ops, ∃ ok,
        e = mk op e.pre e.post ∧ applyRaw ctx ok op e.pre = .ok e.post ∧
        e.pre.messages = w.messages ∧ e.pre.agents = w.agents
  | [], w => by simp [applyOps]
  | op :: rest, w => by
    intro e he
    simp only [applyOps] at he
    split at he
    · obtain ⟨op', hop', ok, h⟩ := applyOps_entries hmk ctx hooks rest w e he
      exact ⟨op', List.mem_cons_of_mem _ hop', ok, h⟩
    · rename_i w' hw'
      rcases List.mem_cons.1 he with rfl | he
      · refine ⟨op, List.mem_cons_self .., hooks.applyOk ctx op w, ?_⟩
        rw [hmk.pre, hmk.post]
        exact ⟨rfl, hw', rfl, rfl⟩
      · obtain ⟨op', hop', ok, h1, h2, h3, h4⟩ := applyOps_entries hmk ctx hooks rest w' e he
        have hp := applyRaw_preserves hw'
        exact ⟨op', List.mem_cons_of_mem _ hop', ok, h1, h2, h3.trans hp.1, h4.trans hp.2⟩

def Outcome.entries : Outcome → List Entry
  | .next _ es => es
  | .rollback es _ _ => es

theorem rollbackProblem_none {scope : Option AuditScope} {hooks : Hooks} {ctx : Ctx}
    {bAt : Nat → Option Boundary} {op : Op} {w : World}
    (h : rollbackProblem scope hooks ctx bAt op w = none) :
    ∀ s, scope = some s → s.problem w op = none := by
  intro s hs
  subst hs
  unfold rollbackProblem at h
  cases hp : s.problem w op with
  | none => rfl
  | some p => simp [Option.bind, hp] at h

theorem checked_none {scope : Option AuditScope} {ctx : Ctx} {op : Op} {w : World}
    (h : checkProblem scope ctx op w = none) :
    (∀ s, scope = some s → s.problem w op = none) ∧ checkProblem none ctx op w = none := by
  cases scope with
  | none => exact ⟨(fun _ h' => nomatch h'), h⟩
  | some s =>
    have := checkProblem_some h
    exact ⟨fun s' hs => by cases hs; exact this.1, this.2⟩

theorem decide_entries {scope : Option AuditScope} {hooks : Hooks}
    {mk : Op → World → World → Entry} (hmk : MkOk mk) (ctx : Ctx) (replaying : Bool)
    (bAt : Nat → Option Boundary) (ops : List Op) (w : World) :
    ∀ e ∈ (decide scope hooks mk ctx replaying bAt ops w).entries, ∃ op,
      e = mk op e.pre e.post ∧ (∀ s, scope = some s → s.problem e.pre op = none) ∧
      (e.post = e.pre ∨
        ∃ ok, applyRaw ctx ok op e.pre = .ok e.post ∧ checkProblem none ctx op e.pre = none) := by
  intro e he
  unfold decide at he
  split at he
  · split at he
    · simp [Outcome.entries] at he
    · split at he
      · rename_i op
        split at he
        · simp [Outcome.entries] at he
        · rename_i hrp
          split at he
          · simp only [Outcome.entries, List.mem_singleton] at he
            subst he
            refine ⟨_, by rw [hmk.pre, hmk.post], ?_, Or.inl (by rw [hmk.pre, hmk.post])⟩
            rw [hmk.pre]
            exact rollbackProblem_none hrp
          · simp [Outcome.entries] at he
      · simp [Outcome.entries] at he
  · dsimp only at he
    split at he
    · simp [Outcome.entries] at he
    · rename_i hc
      simp only [Outcome.entries] at he
      obtain ⟨op, hop, ok, h1, h2, h3, h4⟩ := applyOps_entries hmk ctx hooks _ w e he
      have hmem : op ∈ ops := by
        rcases List.mem_append.1 hop with h | h <;> exact (List.mem_filter.1 h).1
      have hck : checkProblem scope ctx op w = none := by
        have : (ops.any (fun op => (checkProblem scope ctx op w).isSome)) = false := by
          simp only [Bool.or_eq_true, not_or] at hc
          simpa using hc.1.1
        simp only [List.any_eq_false] at this
        simpa using this op hmem
      have hn := checked_none hck
      refine ⟨op, h1, fun s hs => problem_congr h3.symm s op (hn.1 s hs), Or.inr ⟨ok, h2, ?_⟩⟩
      rw [checkProblem_congr h4]
      exact hn.2

theorem decide_good {scope : Option AuditScope} {hooks : Hooks}
    {mk : Op → World → World → Entry} (hmk : MkOk mk) {ctx : Ctx}
    (hs : ∀ op pre post, (mk op pre post).scope = scope)
    (hc : ∀ op pre post, (mk op pre post).ctx = ctx) (replaying : Bool)
    (bAt : Nat → Option Boundary) (ops : List Op) (w : World) :
    ∀ e ∈ (decide scope hooks mk ctx replaying bAt ops w).entries, Good scope e := by
  intro e he
  obtain ⟨op, heq, hp, hpost⟩ := decide_entries hmk ctx replaying bAt ops w e he
  have hsc : e.scope = scope := by rw [heq, hs]
  have hctx : e.ctx = ctx := by rw [heq, hc]
  have hop : e.op = op := by rw [heq, hmk.op]
  exact ⟨hsc, by rw [hop]; exact hp, by rw [hctx, hop]; exact hpost⟩

theorem decide_good' {scope : Option AuditScope} {hooks : Hooks}
    {mk : Op → World → World → Entry} {ctx : Ctx} {replaying : Bool}
    {bAt : Nat → Option Boundary} {ops : List Op} {w : World} {out : Outcome} {e : Entry}
    (hd : decide scope hooks mk ctx replaying bAt ops w = out) (he : e ∈ out.entries)
    (hmk : MkOk mk) (hs : ∀ op pre post, (mk op pre post).scope = scope)
    (hc : ∀ op pre post, (mk op pre post).ctx = ctx) : Good scope e :=
  decide_good hmk hs hc replaying bAt ops w e (hd ▸ he)

theorem runBranch_good (scope : Option AuditScope) (env : Env) (policy : Policy) (b : Nat)
    (replay : Option Replay) :
    ∀ (fuel rev : Nat) (w : World) (decs : List (List Op)) (bounds : List Boundary)
      (es : List Entry), (∀ e ∈ es, Good scope e) →
      ∀ e ∈ (runBranch scope env policy b replay fuel rev w decs bounds es).entries,
        Good scope e
  | 0, _, _, _, _, es => by
    intro hes e he
    simp only [runBranch] at he
    exact hes e he
  | fuel + 1, rev, w, decs, bounds, es => by
    intro hes e he
    simp only [runBranch] at he
    split at he
    · exact hes e he
    · split at he
      · rename_i w' es' hd
        refine runBranch_good scope env policy b replay fuel (rev + 1) _ _ _ (es ++ es') ?_ e he
        intro e' he'
        rcases List.mem_append.1 he' with h | h
        · exact hes e' h
        · exact decide_good' hd h ⟨fun _ _ _ => rfl, fun _ _ _ => rfl, fun _ _ _ => rfl⟩
            (fun _ _ _ => rfl) (fun _ _ _ => rfl)
      · rename_i es' t ivs hd
        simp only at he
        rcases List.mem_append.1 he with h | h
        · exact hes e h
        · exact decide_good' hd h ⟨fun _ _ _ => rfl, fun _ _ _ => rfl, fun _ _ _ => rfl⟩
            (fun _ _ _ => rfl) (fun _ _ _ => rfl)

theorem auditLoop_good (scope : Option AuditScope) (env : Env) (policy : Policy) (w0 : World)
    (fuel : Nat) : ∀ (n b : Nat) (pending : List (Option Replay)) (acc : List Entry),
      (∀ e ∈ acc, Good scope e) →
      ∀ e ∈ auditLoop scope env policy w0 fuel n b pending acc, Good scope e
  | 0, _, _, acc => by intro h; simpa [auditLoop] using h
  | _ + 1, _, [], acc => by intro h; simpa [auditLoop] using h
  | n + 1, b, r :: rest, acc => by
    intro hacc
    simp only [auditLoop]
    refine auditLoop_good scope env policy w0 fuel n (b + 1) _ _ ?_
    intro e he
    rcases List.mem_append.1 he with h | h
    · exact hacc e h
    · exact runBranch_good scope env policy b r fuel 1 w0 [] [] [] (by simp) e h

theorem runAudit_good (scope : Option AuditScope) (env : Env) (policy : Policy) (w0 : World)
    (maxBranches fuel : Nat) :
    ∀ e ∈ runAudit scope env policy w0 maxBranches fuel, Good scope e :=
  auditLoop_good scope env policy w0 fuel maxBranches 0 [none] [] (by simp)

theorem enforced : Enforced := fun s env policy w0 nb fuel e he =>
  (runAudit_good (some s) env policy w0 nb fuel e he).2.1 s rfl

theorem confined : Confined := fun s env policy w0 nb fuel e he =>
  (runAudit_good (some s) env policy w0 nb fuel e he).1

theorem onlyApplied : OnlyApplied := by
  intro scope hooks mk ctx replaying bAt ops w w' es hmk h
  have hmk' : MkOk mk := ⟨fun o p q => (hmk o p q).1, fun o p q => (hmk o p q).2.1,
    fun o p q => (hmk o p q).2.2⟩
  unfold decide at h
  split at h
  · split at h
    · cases h; rfl
    · split at h
      · split at h
        · cases h; rfl
        · split at h
          · cases h
          · cases h; rfl
      · cases h; rfl
  · dsimp only at h
    split at h
    · cases h; rfl
    · cases h
      exact applyOps_chain hmk' ctx hooks _ w

end Scope
