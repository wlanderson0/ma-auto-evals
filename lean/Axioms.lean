import Scope

/-! Run by `check.sh`: each statement has a proof of exactly its type, and every proof
depends only on the standard axioms. -/

open Scope

example : ScopeSound := scopeSound
example : Frame := frame
example : Enforced := enforced
example : Monotone := monotone
example : Meet := meet
example : OnlyApplied := onlyApplied
example : Confined := confined
example : ScopeRespected := scopeRespected
example : G2Fixed := g2Fixed
example : Witness.RunWitness := Witness.runWitness
example : Witness.FrameWitness := Witness.frameWitness
example : Witness.SeatWitness := Witness.seatWitness
example : Witness.MonotoneWitness := Witness.monotoneWitness
example : Witness.MeetWitness := Witness.meetWitness
example : Witness.OnlyAppliedWitness := Witness.onlyAppliedWitness
example : Witness.ReplayMutantWitness := Witness.replayMutantWitness
example : Witness.AgentsMutantWitness := Witness.agentsMutantWitness
example : Witness.RepresentationWitness := Witness.representationWitness
example : Witness.G1Witness := Witness.g1Witness
example : Witness.HaltWitness := Witness.haltWitness
example : Branch.Fidelity := Branch.fidelity
example : Branch.ThroughFork := Branch.throughFork
example : Branch.FailSafe := Branch.failSafe
example : Branch.FailSafeRun := Branch.failSafeRun
example : Branch.CompletedReachedFork := Branch.completedReachedFork
example : Branch.ForkEffect := Branch.forkEffect
example : Branch.IdsOwnAfterFork := Branch.idsOwnAfterFork
example : Branch.ScopeRespectedB := Branch.scopeRespectedB
example : Branch.ChainShape := Branch.chainShape
example : Branch.AuditLink := Branch.auditLink
example : Branch.ScoredIffCompleted := Branch.scoredIffCompleted
example : Branch.ScoredReachedFork := Branch.scoredReachedFork
example : Branch.Witness.RealForkWitness := Branch.Witness.realForkWitness
example : Branch.Witness.bug_ForkRejected := Branch.Witness.bug_forkRejected
example : Branch.Witness.bug_NestedDesync := Branch.Witness.bug_nestedDesync
example : Branch.Witness.bug_NestedSilent := Branch.Witness.bug_nestedSilent
example : Branch.Witness.ScopedForkWitness := Branch.Witness.scopedForkWitness
example : Branch.Witness.VetoWitness := Branch.Witness.vetoWitness
example : Branch.Witness.SameForkWitness := Branch.Witness.sameForkWitness

#print axioms scopeSound
#print axioms frame
#print axioms enforced
#print axioms monotone
#print axioms meet
#print axioms onlyApplied
#print axioms confined
#print axioms scopeRespected
#print axioms g2Fixed
#print axioms Witness.runWitness
#print axioms Witness.frameWitness
#print axioms Witness.seatWitness
#print axioms Witness.monotoneWitness
#print axioms Witness.meetWitness
#print axioms Witness.onlyAppliedWitness
#print axioms Witness.replayMutantWitness
#print axioms Witness.agentsMutantWitness
#print axioms Witness.representationWitness
#print axioms Witness.g1Witness
#print axioms Witness.haltWitness
#print axioms Branch.fidelity
#print axioms Branch.throughFork
#print axioms Branch.failSafe
#print axioms Branch.failSafeRun
#print axioms Branch.completedReachedFork
#print axioms Branch.forkEffect
#print axioms Branch.idsOwnAfterFork
#print axioms Branch.scopeRespectedB
#print axioms Branch.chainShape
#print axioms Branch.auditLink
#print axioms Branch.scoredIffCompleted
#print axioms Branch.scoredReachedFork
#print axioms Branch.Witness.realForkWitness
#print axioms Branch.Witness.bug_forkRejected
#print axioms Branch.Witness.bug_nestedDesync
#print axioms Branch.Witness.bug_nestedSilent
#print axioms Branch.Witness.scopedForkWitness
#print axioms Branch.Witness.vetoWitness
#print axioms Branch.Witness.sameForkWitness
