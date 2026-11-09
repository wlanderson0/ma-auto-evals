import Scope.Proofs.Frame
import Scope.Spec.Witnesses

/-! The headline theorem and the known gaps. -/

namespace Scope

theorem scopeRespected : ScopeRespected := by
  intro s env policy w0 nb fuel e he
  obtain ⟨hsc, hp, hpost⟩ := runAudit_good (some s) env policy w0 nb fuel e he
  refine ⟨hsc, hp s rfl, fun hwf hcarve a ha hl => ?_⟩
  rcases hpost with heq | ⟨ok, happ, hck⟩
  · rw [heq]
  · have hnot : a ∉ e.op.affected e.pre := fun hin => by
      have := (scopeSound s e.pre e.op (hp s rfl)).2.1 a hin
      rw [ha] at this; cases this
    exact frameUpToGaps e.ctx ok e.op e.pre e.post a hwf hck hcarve happ hnot hl

theorem Witness.w0_wf : Witness.w0.WF := by unfold World.WF; decide

open Witness in
/-- G1: `amend_pending` lists only the agents in its new steps, so removing Bob's turn
leaves Bob out of `affected` while his view changes. -/
theorem bug_G1_amend_pending : ¬ Frame := by
  intro hF
  have := hF betweenTurns true (.amendPending [["alice"]] 0) w0
    { w0 with schedule := some ⟨1, [["alice"]]⟩ } "bob" w0_wf (by decide) rfl rfl (by decide)
  have := congrArg AgentView.turns this
  exact absurd this (by decide)

open Witness in
/-- G2: an op with an empty `agent` has an empty `affected`, passes the controller's
targeted check, and is applied to whichever agent's boundary it is given at. -/
theorem bug_G2_empty_agent : ¬ Frame := by
  intro hF
  have := hF atBob true (.replaceGeneration "" "all tests pass" []) w0
    { w0 with own := fun a => if a == "bob" then ["replace_generation"] else [] } "bob" w0_wf
    (by decide) rfl rfl (by decide)
  have := congrArg AgentView.own this
  exact absurd this (by decide)

end Scope
