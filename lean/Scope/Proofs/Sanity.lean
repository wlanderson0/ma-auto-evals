import Scope.Proofs.Main

/-! Proofs of the witness statements in `Spec/Witnesses.lean`. -/

namespace Scope.Witness

theorem runWitness : RunWitness := by unfold RunWitness; decide

theorem frameWitness : FrameWitness :=
  ⟨w0_wf, by decide, rfl, by decide, by decide, _, rfl, by decide⟩

theorem seatWitness : SeatWitness := ⟨by decide, _, rfl, by decide⟩

theorem monotoneWitness : MonotoneWitness := by
  refine ⟨⟨Or.inl rfl, Or.inr ⟨["alice"], ["alice", "bob"], rfl, rfl, ?_⟩⟩, by decide, by decide⟩
  intro n hn
  simp only [List.mem_singleton] at hn
  simp [hn]

theorem meetWitness : MeetWitness := by unfold MeetWitness; decide

theorem onlyAppliedWitness : OnlyAppliedWitness := by unfold OnlyAppliedWitness; decide

theorem replayMutantWitness : ReplayMutantWitness := by unfold ReplayMutantWitness; decide

theorem agentsMutantWitness : AgentsMutantWitness := by
  intro h
  have := h seat w0 dropToAll (by decide) "bob" (by decide)
  exact absurd this (by decide)

theorem representationWitness : RepresentationWitness := by unfold RepresentationWitness; decide

theorem haltWitness : HaltWitness := by unfold HaltWitness; decide

end Scope.Witness
