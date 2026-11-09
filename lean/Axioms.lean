import Scope

/-! Run by `check.sh`: each statement has a proof of exactly its type, and every proof
depends only on the standard axioms. -/

open Scope

example : ScopeSound := scopeSound
example : FrameUpToGaps := frameUpToGaps
example : Enforced := enforced
example : Monotone := monotone
example : Meet := meet
example : OnlyApplied := onlyApplied
example : Confined := confined
example : ScopeRespected := scopeRespected
example : ¬ Frame := bug_G1_amend_pending
example : ¬ Frame := bug_G2_empty_agent
example : Witness.RunWitness := Witness.runWitness
example : Witness.FrameWitness := Witness.frameWitness
example : Witness.SeatWitness := Witness.seatWitness
example : Witness.MonotoneWitness := Witness.monotoneWitness
example : Witness.MeetWitness := Witness.meetWitness
example : Witness.OnlyAppliedWitness := Witness.onlyAppliedWitness
example : Witness.ReplayMutantWitness := Witness.replayMutantWitness
example : Witness.AgentsMutantWitness := Witness.agentsMutantWitness
example : Witness.RepresentationWitness := Witness.representationWitness
example : Witness.HaltWitness := Witness.haltWitness

#print axioms scopeSound
#print axioms frameUpToGaps
#print axioms enforced
#print axioms monotone
#print axioms meet
#print axioms onlyApplied
#print axioms confined
#print axioms scopeRespected
#print axioms bug_G1_amend_pending
#print axioms bug_G2_empty_agent
#print axioms Witness.runWitness
#print axioms Witness.frameWitness
#print axioms Witness.seatWitness
#print axioms Witness.monotoneWitness
#print axioms Witness.meetWitness
#print axioms Witness.onlyAppliedWitness
#print axioms Witness.replayMutantWitness
#print axioms Witness.agentsMutantWitness
#print axioms Witness.representationWitness
#print axioms Witness.haltWitness
