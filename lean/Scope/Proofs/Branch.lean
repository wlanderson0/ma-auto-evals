import Scope.Proofs.Main
import Scope.Spec.BranchWitnesses

/-! Rollback proofs: the statements that follow from one boundary's structure. -/

namespace Scope.Branch

theorem tick_ended {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {replay : Option Replay} {c : Cfg} {x : Status} (h : c.ended = some x) :
    tick scope env policy b replay c = c := by
  simp [tick, h]

theorem run_ended {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {replay : Option Replay} : ∀ (n : Nat) {c : Cfg} {x : Status}, c.ended = some x →
    run scope env policy b replay n c = c
  | 0, _, _, _ => rfl
  | n + 1, c, x, h => by
    simp only [run, tick_ended h]
    exact run_ended n h

theorem scoredIffCompleted : ScoredIffCompleted := by
  intro outs o
  unfold scored
  cases adopted outs with
  | none => simp
  | some o' =>
    by_cases hc : o'.ended = some .completed
    · simp only [hc, beq_self_eq_true, ↓reduceIte, Option.some.injEq]
      exact ⟨fun h => ⟨h, h ▸ hc⟩, fun h => h.1⟩
    · have : (o'.ended == some Status.completed) = false := by simpa using hc
      simp only [this, Bool.false_eq_true, ↓reduceIte]
      constructor
      · intro h; cases h
      · rintro ⟨h1, h2⟩; cases h1; exact absurd h2 hc

theorem desynced_of_mismatch {env : Env} {R : Replay} {rev : Nat} {st : State}
    (hle : rev ≤ R.fork)
    (hfp : ∀ s, R.steps[rev - 1]? = some s → s.fingerprint ≠ env.fingerprint st) :
    desynced env (some R) rev st = true := by
  unfold desynced
  simp only [hle, ↓reduceIte]
  cases h : R.steps[rev - 1]? with
  | none => rfl
  | some s => simpa using hfp s h

theorem failSafe : FailSafe := by
  intro scope env policy b R c hend hle hdone hcut hfp
  have hd := desynced_of_mismatch hle hfp
  simp [tick, hend, hcut, hdone, hd]

theorem tick_completed {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {R : Replay} {c : Cfg} (hend : c.ended = none)
    (h : (tick scope env policy b (some R) c).ended = some .completed) :
    R.fork ≤ (tick scope env policy b (some R) c).rev - 1 := by
  unfold tick at h ⊢
  simp only [hend] at h ⊢
  split at h
  · rename_i x _
    cases x <;> simp [Cut.status] at h
  · split at h
    · simp only [finished] at h ⊢
      split at h <;> simp_all
    · split at h
      · simp at h
      · split at h <;> simp_all

theorem run_completed {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {R : Replay} : ∀ (n : Nat) (c : Cfg),
    (c.ended = some .completed → R.fork ≤ c.rev - 1) →
    (run scope env policy b (some R) n c).ended = some .completed →
      R.fork ≤ (run scope env policy b (some R) n c).rev - 1
  | 0, _, h => h
  | n + 1, c, h => by
    simp only [run]
    apply run_completed n
    intro ht
    cases hc : c.ended with
    | none => exact tick_completed hc ht
    | some x => rw [tick_ended hc] at ht ⊢; exact h ht

theorem completedReachedFork : CompletedReachedFork := by
  intro scope env policy b R fuel sh s0
  dsimp only
  intro h
  unfold branch at h ⊢
  simp only at h ⊢
  have key := run_completed (scope := scope) (env := env) (policy := policy) (b := b) (R := R) fuel
    (start env sh b s0) (fun h' => by simp [start] at h')
  split at h
  · simp at h
  · rename_i hn
    simp only [hn, Bool.false_eq_true, ↓reduceIte]
    exact key h

theorem tick_good {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {replay : Option Replay} {c : Cfg} :
    ∀ e ∈ (tick scope env policy b replay c).entries, e ∈ c.entries ∨ Good scope e := by
  intro e he
  unfold tick at he
  split at he
  · exact Or.inl he
  · dsimp only at he
    split at he
    · exact Or.inl he
    · split at he
      · exact Or.inl he
      · split at he
        · exact Or.inl he
        · split at he
          · rename_i es t ivs hd
            rcases List.mem_append.1 he with h | h
            · exact Or.inl h
            · exact Or.inr (decide_good' hd (by simpa [Outcome.entries] using h)
                ⟨fun _ _ _ => rfl, fun _ _ _ => rfl, fun _ _ _ => rfl⟩
                (fun _ _ _ => rfl) (fun _ _ _ => rfl))
          · rename_i w es hd
            rcases List.mem_append.1 he with h | h
            · exact Or.inl h
            · exact Or.inr (decide_good' hd (by simpa [Outcome.entries] using h)
                ⟨fun _ _ _ => rfl, fun _ _ _ => rfl, fun _ _ _ => rfl⟩
                (fun _ _ _ => rfl) (fun _ _ _ => rfl))

theorem run_good {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {replay : Option Replay} : ∀ (n : Nat) (c : Cfg), (∀ e ∈ c.entries, Good scope e) →
    ∀ e ∈ (run scope env policy b replay n c).entries, Good scope e
  | 0, _, h => h
  | n + 1, c, h => by
    simp only [run]
    apply run_good n
    intro e he
    rcases tick_good e he with h' | h'
    · exact h e h'
    · exact h'

theorem branch_good {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {replay : Option Replay} {fuel : Nat} {sh : Shared} {s0 : State} :
    ∀ e ∈ (branch scope env policy b replay fuel sh s0).entries, Good scope e := by
  intro e he
  unfold branch at he
  simp only at he
  have := run_good (scope := scope) (env := env) (policy := policy) (b := b) (replay := replay)
    fuel (start env sh b s0) (by simp [start])
  split at he <;> exact this e he

theorem auditLoop_good {scope : Option AuditScope} {env : Env} {policy : Policy} {s0 : State}
    {fuel : Nat} : ∀ (n b : Nat) (r : Option Replay) (sh : Shared) (acc : List Cfg),
    (∀ o ∈ acc, ∀ e ∈ o.entries, Good scope e) →
    ∀ o ∈ auditLoop scope env policy s0 fuel n b r sh acc, ∀ e ∈ o.entries, Good scope e
  | 0, _, _, _, acc, h => by simpa [auditLoop] using h
  | n + 1, b, r, sh, acc, h => by
    have hacc : ∀ o ∈ acc ++ [branch scope env policy b r fuel sh s0], ∀ e ∈ o.entries,
        Good scope e := by
      intro o ho
      rcases List.mem_append.1 ho with h' | h'
      · exact h o h'
      · rw [List.mem_singleton.1 h']; exact branch_good
    simp only [auditLoop]
    split
    · exact auditLoop_good n (b + 1) _ _ _ hacc
    · exact hacc

theorem scopeRespectedB : ScopeRespectedB := by
  intro s env policy s0 sh nb fuel o ho e he
  obtain ⟨hsc, hp, hpost⟩ := auditLoop_good nb 0 none sh [] (by simp) o ho e he
  refine ⟨hsc, hp s rfl, fun hwf hcarve a ha => ?_⟩
  rcases hpost with heq | ⟨ok, happ, hck⟩
  · rw [heq]
  · have hnot : a ∉ e.op.affected e.pre := fun hin => by
      have := (scopeSound s e.pre e.op (hp s rfl)).2.1 a hin
      rw [ha] at this; cases this
    exact frame e.ctx ok e.op e.pre e.post a hwf hck hcarve happ hnot

/-- A branch records a rollback only when it ends rolled back. -/
def RollbackInv (c : Cfg) : Prop := c.rollback ≠ none → c.ended = some .rolledBack

theorem tick_rollbackInv {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {replay : Option Replay} {c : Cfg} (h : RollbackInv c) :
    RollbackInv (tick scope env policy b replay c) := by
  cases hc : c.ended with
  | some x => rw [tick_ended hc]; exact h
  | none =>
    have hr : c.rollback = none := by
      cases hro : c.rollback with
      | none => rfl
      | some _ => have := h (by simp [hro]); rw [hc] at this; cases this
    unfold RollbackInv tick
    simp only [hc]
    split
    · simp [hr]
    · split
      · simp [hr]
      · split
        · simp [hr]
        · split <;> simp

theorem run_rollbackInv {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {replay : Option Replay} : ∀ (n : Nat) (c : Cfg), RollbackInv c →
    RollbackInv (run scope env policy b replay n c)
  | 0, _, h => h
  | n + 1, _, h => run_rollbackInv n _ (tick_rollbackInv h)

theorem branch_rolledBack {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {replay : Option Replay} {fuel : Nat} {sh : Shared} {s0 : State}
    (h : (branch scope env policy b replay fuel sh s0).rollback ≠ none) :
    (branch scope env policy b replay fuel sh s0).ended = some .rolledBack := by
  have hinv := run_rollbackInv (scope := scope) (env := env) (policy := policy) (b := b)
    (replay := replay) fuel (start env sh b s0) (by simp [RollbackInv, start])
  unfold branch at h ⊢
  simp only at h ⊢
  split
  · rename_i hn
    simp only [hn, ↓reduceIte] at h
    have := hinv h
    simp [this] at hn
  · rename_i hn
    simp only [hn, Bool.false_eq_true, ↓reduceIte] at h
    exact hinv h

theorem auditLoop_shape {scope : Option AuditScope} {env : Env} {policy : Policy} {s0 : State}
    {fuel : Nat} : ∀ (n b : Nat) (r : Option Replay) (sh : Shared) (acc : List Cfg),
    (∀ o ∈ acc, o.ended = some .rolledBack) →
    ∃ pre tail, auditLoop scope env policy s0 fuel n b r sh acc = pre ++ tail ∧
      (∀ o ∈ pre, o.ended = some .rolledBack) ∧ tail.length ≤ 1
  | 0, _, _, _, acc, h => ⟨acc, [], by simp [auditLoop], h, by simp⟩
  | n + 1, b, r, sh, acc, h => by
    simp only [auditLoop]
    split
    · rename_i t ivs hr
      apply auditLoop_shape n (b + 1)
      intro o ho
      rcases List.mem_append.1 ho with h' | h'
      · exact h o h'
      · rw [List.mem_singleton.1 h']; exact branch_rolledBack (by rw [hr]; simp)
    · exact ⟨acc, [_], rfl, h, by simp⟩

theorem chainShape : ChainShape := by
  intro scope env policy s0 sh nb fuel
  dsimp only
  intro i hi
  obtain ⟨pre, tail, heq, hpre, htail⟩ :=
    auditLoop_shape (scope := scope) (env := env) (policy := policy) (s0 := s0) (fuel := fuel)
      nb 0 none sh [] (by simp)
  have hlen : i < pre.length := by
    have : (runAudit scope env policy s0 sh nb fuel).length = pre.length + tail.length := by
      simp [runAudit, heq]
    omega
  have : (runAudit scope env policy s0 sh nb fuel)[i] = pre[i] := by
    simp only [runAudit, heq]
    exact List.getElem_append_left hlen
  rw [this]
  exact hpre _ (List.getElem_mem hlen)

end Scope.Branch
