import Scope.Proofs.Frame
import Scope.Spec.Witnesses

/-! The headline theorem, and the G2 fix. -/

namespace Scope

theorem scopeRespected : ScopeRespected := by
  intro s env policy w0 nb fuel e he
  obtain ⟨hsc, hp, hpost⟩ := runAudit_good (some s) env policy w0 nb fuel e he
  refine ⟨hsc, hp s rfl, fun hwf hcarve a ha => ?_⟩
  rcases hpost with heq | ⟨ok, happ, hck⟩
  · rw [heq]
  · have hnot : a ∉ e.op.affected e.pre := fun hin => by
      have := (scopeSound s e.pre e.op (hp s rfl)).2.1 a hin
      rw [ha] at this; cases this
    exact frame e.ctx ok e.op e.pre e.post a hwf hck hcarve happ hnot

theorem Witness.w0_wf : Witness.w0.WF := by unfold World.WF; decide

/-- G2 is fixed: an op naming no agent is refused, and one that passes at an agent's
boundary names exactly that agent. -/
theorem g2Fixed : G2Fixed := by
  intro ctx op w b
  refine ⟨fun h => checkProblem_empty_agent h, fun hb hf hck hs => ?_⟩
  obtain ⟨x, hx⟩ := Option.isSome_iff_exists.1 hs
  have hxe : x ≠ "" := fun e => checkProblem_empty_agent (e ▸ hx) hck
  have hag : op.agents = [x] := by
    cases op <;> simp_all [Op.agentField?, Op.agents, agentTuple_of_ne hxe]
  have hall := targeted_ok hck hf hb (by simp [hag])
  rw [hag] at hall ⊢
  simp at hall
  rw [hall]

end Scope
