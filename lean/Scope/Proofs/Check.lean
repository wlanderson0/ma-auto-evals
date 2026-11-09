import Scope.Spec.Statements

namespace Scope
namespace AuditScope

theorem problemList_none (s : AuditScope) (w : World) :
    ∀ ivs : List Op, s.problemList w ivs = none ↔ ∀ iv ∈ ivs, s.problem w iv = none
  | [] => by simp [problemList]
  | iv :: rest => by
    have ih := problemList_none s w rest
    cases h : s.problem w iv <;> simp [problemList, h, ih]

theorem ownProblem_none (s : AuditScope) (w : World) (op : Op) :
    s.ownProblem w op = none ↔
      s.allowsOp op.name = true ∧ ∀ a ∈ op.affected w, s.allowsAgent a = true := by
  unfold ownProblem
  by_cases h : s.allowsOp op.name = true
  · simp [h, List.filter_eq_nil_iff]
  · simp [h]

theorem problem_none (s : AuditScope) (w : World) (op : Op) :
    s.problem w op = none ↔
      s.ownProblem w op = none ∧ ∀ iv ∈ op.interventions, s.problem w iv = none := by
  cases op with
  | rollback t ivs =>
    simp only [problem, Op.interventions]
    cases h : s.ownProblem w (.rollback t ivs) <;> simp [problemList_none]
  | _ => simp [problem, Op.interventions]

end AuditScope

theorem sizeOf_lt_of_intervention {op iv : Op} (h : iv ∈ op.interventions) :
    sizeOf iv < sizeOf op := by
  cases op with
  | rollback t ivs =>
    simp only [Op.interventions] at h
    have := List.sizeOf_lt_of_mem h
    simp only [Op.rollback.sizeOf_spec]
    omega
  | _ => simp [Op.interventions] at h

theorem scopeSound : ScopeSound := by
  intro s w op h
  have h' := (AuditScope.problem_none s w op).1 h
  have h1 := (AuditScope.ownProblem_none s w op).1 h'.1
  exact ⟨h1.1, h1.2, h'.2⟩

namespace AuditScope

theorem allowsOp_mono {s t : AuditScope} (h : s.Le t) (n : String) :
    s.allowsOp n = true → t.allowsOp n = true := by
  rcases h.1 with h1 | ⟨l, l', h1, h2, h3⟩
  · simp [allowsOp, h1]
  · simpa [allowsOp, h1, h2] using h3 n

theorem allowsAgent_mono {s t : AuditScope} (h : s.Le t) (a : Agent) :
    s.allowsAgent a = true → t.allowsAgent a = true := by
  rcases h.2 with h1 | ⟨l, l', h1, h2, h3⟩
  · simp [allowsAgent, h1]
  · simpa [allowsAgent, h1, h2] using h3 a

theorem problem_mono {s t : AuditScope} (h : s.Le t) (w : World) :
    ∀ op, s.problem w op = none → t.problem w op = none
  | op, hp => by
    rw [problem_none, ownProblem_none] at hp ⊢
    refine ⟨⟨allowsOp_mono h _ hp.1.1, fun a ha => allowsAgent_mono h a (hp.1.2 a ha)⟩, ?_⟩
    intro iv hiv
    exact problem_mono h w iv (hp.2 iv hiv)
termination_by op => sizeOf op
decreasing_by exact sizeOf_lt_of_intervention hiv

theorem allowsOp_meet (s t : AuditScope) (n : String) :
    (s.meet t).allowsOp n = (s.allowsOp n && t.allowsOp n) := by
  simp only [allowsOp, meet]
  cases s.ops <;> cases t.ops <;> simp [meetLimit]

theorem allowsAgent_meet (s t : AuditScope) (a : Agent) :
    (s.meet t).allowsAgent a = (s.allowsAgent a && t.allowsAgent a) := by
  simp only [allowsAgent, meet]
  cases s.agents <;> cases t.agents <;> simp [meetLimit]

theorem problem_meet (s t : AuditScope) (w : World) :
    ∀ op, (s.meet t).problem w op = none ↔ (s.problem w op = none ∧ t.problem w op = none)
  | op => by
    rw [problem_none, problem_none, problem_none, ownProblem_none, ownProblem_none,
      ownProblem_none]
    simp only [allowsOp_meet, allowsAgent_meet, Bool.and_eq_true]
    constructor
    · rintro ⟨⟨⟨h1, h2⟩, h3⟩, h4⟩
      refine ⟨⟨⟨h1, fun a ha => (h3 a ha).1⟩, fun iv hiv => ((problem_meet s t w iv).1 (h4 iv hiv)).1⟩,
              ⟨⟨h2, fun a ha => (h3 a ha).2⟩, fun iv hiv => ((problem_meet s t w iv).1 (h4 iv hiv)).2⟩⟩
    · rintro ⟨⟨⟨h1, h3⟩, h4⟩, ⟨⟨h2, h5⟩, h6⟩⟩
      exact ⟨⟨⟨h1, h2⟩, fun a ha => ⟨h3 a ha, h5 a ha⟩⟩,
        fun iv hiv => (problem_meet s t w iv).2 ⟨h4 iv hiv, h6 iv hiv⟩⟩
termination_by op => sizeOf op
decreasing_by all_goals exact sizeOf_lt_of_intervention hiv

end AuditScope

end Scope

namespace Scope
theorem monotone : Monotone := fun _ _ w op h hp => AuditScope.problem_mono h w op hp
theorem meet : Meet := fun s t w op => AuditScope.problem_meet s t w op
end Scope
