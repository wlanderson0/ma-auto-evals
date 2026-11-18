import Scope.Proofs.BranchAudit

/-! The rollback witnesses hold, by evaluation in the kernel. -/

namespace Scope.Branch.Witness

theorem realForkWitness : RealForkWitness := by unfold RealForkWitness; decide
theorem bug_forkRejected : bug_ForkRejected := by unfold bug_ForkRejected; decide
theorem bug_nestedDesync : bug_NestedDesync := by unfold bug_NestedDesync; decide
theorem bug_nestedSilent : bug_NestedSilent := by unfold bug_NestedSilent; decide
theorem scopedForkWitness : ScopedForkWitness := by unfold ScopedForkWitness; decide
theorem vetoWitness : VetoWitness := by unfold VetoWitness; decide
theorem sameForkWitness : SameForkWitness := by unfold SameForkWitness; decide
theorem t8Witness : T8Witness :=
  ⟨⟨fun _ _ _ _ _ => rfl, fun _ _ _ _ => rfl⟩, ⟨fun _ _ _ _ _ => rfl, fun _ _ _ _ => rfl⟩,
   ⟨fun _ _ _ _ _ => rfl, fun _ _ _ _ => rfl⟩, by decide⟩

end Scope.Branch.Witness
