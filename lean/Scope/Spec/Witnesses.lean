import Scope.Spec.Statements

/-!
# Witness statements (frozen)

What the sanity witnesses in `Proofs/Sanity.lean` must show, stated here so the gate pins
them: the theorems' hypotheses can all hold at once, on an op with a strict, non-empty
`affected`; the frame is precise; the representation choices matter; and mutated
definitions break what they should.
-/

namespace Scope.Witness

def town : Channel := ⟨"town", ["alice", "bob", "carol"], ["alice", "bob", "carol"]⟩
def m1 : Message := ⟨"m1", "town", "alice", ["bob", "carol"], "hi"⟩

/-- Alice, Bob and Carol on one channel; Alice's message to the others is not yet read;
Alice then Bob are scheduled. -/
def w0 : World where
  agents := ["alice", "bob", "carol"]
  channels := [town]
  routes := none
  messages := [m1]
  schedule := some ⟨0, [["alice"], ["bob"]]⟩

def cands : List Agent := ["alice", "bob", "carol"]
def atAlice : Ctx := ⟨.generation, 1, some "alice", none⟩
def atBob : Ctx := ⟨.generation, 5, some "bob", none⟩
def betweenTurns : Ctx := ⟨.betweenTurns, 5, none, none⟩

def seat : AuditScope := { ops := none, agents := some ["alice"] }
def seatBob : AuditScope := { ops := none, agents := some ["alice", "bob"] }
def dropToBob : Op := .dropMessage "m1" (some ["bob"])
def dropToAll : Op := .dropMessage "m1" none
def okHooks : Hooks := { extra := fun _ _ _ => none, applyOk := fun _ _ _ => true }

def env : Env where
  ctx := fun _ _ _ => atAlice
  step := fun _ _ w => w
  stop := fun _ rev _ => rev > 1
  hooks := fun _ => okHooks

def policy : Policy := fun _ rev _ => if rev == 1 then [dropToBob] else []

def record (op : Op) (pre post : World) : Entry :=
  { branch := 0, revision := 1, source := .live, scope := none, ctx := atAlice, op := op,
    pre := pre, post := post }

def appliedCount : Outcome → Nat
  | .next _ es => es.length
  | .rollback es _ _ => es.length

/-- The run theorems and the headline are about a non-empty log: an audit under
`{alice, bob}` applies one op, a drop to Bob, which passes the scope, has
`affected = [bob]`, no gap leak, a well-formed world, and changes Bob's view only. -/
def RunWitness : Prop :=
  let log := runAudit (some seatBob) env policy w0 1 3
  log.map (·.op.name) = ["drop_message"] ∧
    log.map (fun e => changedAgents e.pre e.post cands) = [["bob"]] ∧
    log.map (fun e => e.op.affected e.pre) = [["bob"]] ∧
    log.map (fun e => gapLeak e.ctx e.pre e.op) = [[]] ∧
    log.map (fun e => Decidable.decide ((e.pre.messages.map (·.id)).Nodup)) = [true] ∧
    log.map (fun e => seatBob.problem e.pre e.op) = [none]

/-- Every hypothesis of `FrameUpToGaps` holds for the drop to Bob, and its conclusion is
not a no-op: Bob changes, Carol (outside `affected` and `gapLeak`) does not. -/
def FrameWitness : Prop :=
  w0.WF ∧ checkProblem none atAlice dropToBob w0 = none ∧ carvedOut dropToBob = false ∧
    "carol" ∉ dropToBob.affected w0 ∧ "carol" ∉ gapLeak atAlice w0 dropToBob ∧
    ∃ w', applyRaw atAlice true dropToBob w0 = .ok w' ∧ changedAgents w0 w' cands = ["bob"]

/-- Precision: an op on the seat is accepted under the seat scope and lands on the seat. -/
def SeatWitness : Prop :=
  checkProblem (some seat) atAlice (.replaceGeneration "alice" "ship it" []) w0 = none ∧
    ∃ w', applyRaw atAlice true (.replaceGeneration "alice" "ship it" []) w0 = .ok w' ∧
      changedAgents w0 w' cands = ["alice"]

/-- `Monotone` is strict: a wider scope accepts what a narrower one rejects. -/
def MonotoneWitness : Prop :=
  seat.Le seatBob ∧ seat.problem w0 dropToBob ≠ none ∧ seatBob.problem w0 dropToBob = none

/-- `Meet` is not trivial: the meet of an op limit and an agent limit accepts an op both
accept and rejects one only one of them accepts. -/
def MeetWitness : Prop :=
  let s : AuditScope := { ops := some ["drop_message"], agents := none }
  let t : AuditScope := { ops := none, agents := some ["bob"] }
  (s.meet t).problem w0 dropToBob = none ∧ (s.meet t).problem w0 dropToAll ≠ none ∧
    s.problem w0 dropToAll = none

/-- `OnlyApplied` is about real batches: of a rewrite of a missing message and a drop, the
controller applies the drop only. -/
def OnlyAppliedWitness : Prop :=
  appliedCount (decide (some seatBob) okHooks record atAlice false (fun _ => none)
    [.rewriteMessage "missing" none "x", dropToBob] w0) = 1

/-- A controller that skips the checks while replaying applies an out-of-scope op that the
real one rejects, so `Enforced` depends on the replay check. -/
def decideSkippingReplayCheck (scope : Option AuditScope) (hooks : Hooks)
    (mk : Op → World → World → Entry) (ctx : Ctx) (replaying : Bool)
    (boundaryAt : Nat → Option Boundary) (ops : List Op) (w : World) : Outcome :=
  if replaying then
    let r := applyOps mk ctx hooks ops w
    .next r.1 r.2
  else decide scope hooks mk ctx replaying boundaryAt ops w

def ReplayMutantWitness : Prop :=
  seat.problem w0 dropToAll ≠ none ∧
    appliedCount (decideSkippingReplayCheck (some seat) okHooks record atAlice true
      (fun _ => none) [dropToAll] w0) = 1 ∧
    appliedCount (decide (some seat) okHooks record atAlice true (fun _ => none)
      [dropToAll] w0) = 0

/-- A check that ignores the agent limit breaks property 1. -/
def problemIgnoringAgents (s : AuditScope) (op : Op) : Option ScopeProblem :=
  if s.allowsOp op.name then none else some (.opOutside op.name)

def AgentsMutantWitness : Prop :=
  ¬ ∀ (s : AuditScope) (w : World) (op : Op), problemIgnoringAgents s op = none →
      ∀ a ∈ op.affected w, s.allowsAgent a = true

/-- Representation: `none` vs `some []` for scope limits and for message recipients; a
nested rollback is checked through both levels and refused as a payload. -/
def RepresentationWitness : Prop :=
  ({ ops := none, agents := none } : AuditScope).problem w0 .halt = none ∧
  ({ ops := some [], agents := none } : AuditScope).problem w0 .halt ≠ none ∧
  dropToAll.affected w0 = ["bob", "carol"] ∧
  (Op.dropMessage "m1" (some [])).affected w0 = [] ∧
  seatBob.problem w0 (.rollback 1 [.rollback 1 [.dropMessage "m1" (some ["carol"])]]) =
    some (.agentsOutside "rollback" ["carol"]) ∧
  rollbackProblem (some { ops := none, agents := none }) okHooks atBob (fun _ => some .generation)
    (.rollback 1 [.rollback 1 []]) w0 = some .invalid

/-- D1 is needed: `halt` lists no agents but changes every agent's view. -/
def HaltWitness : Prop :=
  (Op.halt).affected w0 = [] ∧ changedAgents w0 { w0 with halted := true } cands = cands

end Scope.Witness
