import Scope.Proofs.BranchFork

/-! An audit is a chain of branches, each replaying its parent (properties 7a', 7c). -/

namespace Scope.Branch

theorem auditLoop_link {scope : Option AuditScope} {env : Env} {policy : Policy} {s0 : State}
    {fuel : Nat} : ∀ (n b : Nat) (r : Option Replay) (sh : Shared) (acc : List Cfg),
    ∃ L, auditLoop scope env policy s0 fuel n b r sh acc = acc ++ L ∧
      (∀ o, L.head? = some o → o = branch scope env policy b r fuel sh s0) ∧
      ∀ i (h : i + 1 < L.length),
        L[i + 1] = branch scope env policy (b + (i + 1)) (childReplay L[i]) fuel L[i].shared s0
  | 0, _, _, _, acc => ⟨[], by simp [auditLoop], by simp, fun i h => by simp at h⟩
  | n + 1, b, r, sh, acc => by
    simp only [auditLoop]
    split
    · rename_i t ivs hro
      obtain ⟨L', hL', hhead, hidx⟩ := auditLoop_link n (b + 1)
        (some { steps := (branch scope env policy b r fuel sh s0).steps.take t, fork := t,
                interventions := ivs })
        (branch scope env policy b r fuel sh s0).shared
        (acc ++ [branch scope env policy b r fuel sh s0])
      refine ⟨branch scope env policy b r fuel sh s0 :: L', by rw [hL']; simp, by simp, ?_⟩
      intro i h
      cases i with
      | zero =>
        have hne : 0 < L'.length := by simp at h; omega
        have := hhead L'[0] (by cases L' with
          | nil => simp at hne
          | cons x xs => rfl)
        simp only [List.getElem_cons_succ, List.getElem_cons_zero, Nat.zero_add]
        rw [this]
        simp [childReplay, hro]
      | succ j =>
        simp only [List.getElem_cons_succ]
        rw [hidx j (by simp at h; omega)]
        congr 1
        omega
    · exact ⟨[branch scope env policy b r fuel sh s0], rfl, by simp, fun i h => by simp at h⟩

theorem auditLink : AuditLink := by
  intro scope env policy s0 sh nb fuel
  obtain ⟨L, hL, hhead, hidx⟩ := auditLoop_link (scope := scope) (env := env) (policy := policy)
    (s0 := s0) (fuel := fuel) nb 0 none sh []
  simp only [List.nil_append] at hL
  simp only [runAudit, hL]
  refine ⟨hhead, fun i h => ?_⟩
  rw [hidx i h, Nat.zero_add]

theorem branch_rev_pos {scope : Option AuditScope} {env : Env} {policy : Policy} {b : Nat}
    {replay : Option Replay} {fuel : Nat} {sh : Shared} {s0 : State} :
    1 ≤ (branch scope env policy b replay fuel sh s0).rev := by
  have := run_rev_mono (scope := scope) (env := env) (policy := policy) (b := b) (replay := replay)
    fuel (start env sh b s0)
  unfold branch; simp only; split <;> exact this

theorem scoredReachedFork : ScoredReachedFork := by
  intro scope env policy s0 sh nb fuel
  dsimp only
  intro o ho
  have hc := ((scoredIffCompleted _ o).1 ho).2
  refine ⟨hc, fun i h R heq hR => ?_⟩
  have hl := (auditLink scope env policy s0 sh nb fuel).2 i h
  rw [hR] at hl
  rw [heq] at hl
  subst hl
  have := completedReachedFork scope env policy (i + 1) R fuel _ s0 hc
  have hp := branch_rev_pos (scope := scope) (env := env) (policy := policy) (b := i + 1)
    (replay := some R) (fuel := fuel)
    (sh := (runAudit scope env policy s0 sh nb fuel)[i].shared) (s0 := s0)
  omega

end Scope.Branch
