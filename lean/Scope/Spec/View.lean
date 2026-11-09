import Scope.Spec.Model

/-!
# What each agent sees (specification)

`view w a` is a specification definition: it has no Python counterpart and is defined
from the world alone, never from `Op.affected`. It collects everything an op can
directly change for agent `a`, under the decisions recorded in `lean/scope-map.md`:

* D2 (a): deliveries are what the delivery overlay changes. A post the agent's model
  makes is that agent's own output; its spread is for the later information-flow work.
* D3: an agent's pending turns, with their positions, are part of its view.
* `halted` is shared by every agent: halting ends everyone's remaining turns (D1).
* `voiced`: what other agents are shown as `a`'s words that `a` never wrote (an
  injection as `a`, a rewrite of `a`'s message). This is the "sender by attribution" of
  ops.py:311-315, 351-352. Withholding `a`'s message is not in it: it puts no words in
  `a`'s mouth, and `drop_message` does not count the sender (ops.py:325-333).

Lists are compared as lists. Python's delivery sorts by message sequence
(`execution/control.py:488`), which the model does not hold; the differential tests
compare inboxes as sets.

`inbox` follows Orbit's `read_messages` (A1, `communication/runtime.py:281-303`) and
`DeliveryOverlay.deliver` (`delivery.py:37-53`), unioned over the channels `a` can read.
-/

namespace Scope

namespace World

def canRead (w : World) (a : Agent) (c : ChannelId) : Bool :=
  match w.findChannel c with
  | none => false
  | some ch => ch.readers.contains a

/-- `DeliveryOverlay.rewrite_for`, delivery.py:55-58: a recipient-specific rewrite wins
over an all-recipients one. -/
def rewriteFor (w : World) (id : MsgId) (a : Agent) : Option String :=
  match w.overlay.rewrites.find? (·.1 == (id, some a)) with
  | some (_, c) => some c
  | none => (w.overlay.rewrites.find? (·.1 == (id, none))).map (·.2)

def dropped (w : World) (id : MsgId) (a : Agent) : Bool :=
  w.overlay.drops.contains (id, some a) || w.overlay.drops.contains (id, none)

/-- Canonical messages Orbit selects for `a`'s reads (A1). -/
def selected (w : World) (a : Agent) : List Message :=
  w.messages.filter fun m =>
    w.canRead a m.channel && (m.sender == a || (m.recipients.contains a && w.routeAllowed m.sender a))

/-- Every channel message `a` is shown, after the overlay. -/
def inbox (w : World) (a : Agent) : List Message :=
  let shown := (w.selected a).filterMap fun m =>
    if m.sender == a then some m
    else if w.dropped m.id a then none
    else match w.rewriteFor m.id a with
      | some c => some { m with content := c }
      | none => some m
  shown ++ w.overlay.injections.filter (fun m => w.canRead a m.channel && m.recipients.contains a)

def indicesOf (a : Agent) : List Agent → Nat → List Nat
  | [], _ => []
  | x :: rest, j => (if x == a then [j] else []) ++ indicesOf a rest (j + 1)

def positions (a : Agent) : List (List Agent) → Nat → List (Nat × Nat)
  | [], _ => []
  | step :: rest, i => (indicesOf a step 0).map (fun j => (i, j)) ++ positions a rest (i + 1)

/-- `(step, slot)` of each of `a`'s pending turns. -/
def turns (w : World) (a : Agent) : List (Nat × Nat) :=
  match w.schedule with
  | none => []
  | some s => positions a s.pending 0

/-- `(reader, message)` for every message a setup agent is shown as `a`'s that is not one
of `a`'s canonical messages as sent. -/
def voiced (w : World) (a : Agent) : List (Agent × Message) :=
  w.agents.flatMap fun r =>
    ((w.inbox r).filter (fun m => m.sender == a && !w.messages.contains m)).map (r, ·)

end World

structure AgentView where
  own : List String
  inbox : List Message
  turns : List (Nat × Nat)
  halted : Bool
  voiced : List (Agent × Message)
  deriving DecidableEq, Repr

def view (w : World) (a : Agent) : AgentView :=
  { own := w.own a, inbox := w.inbox a, turns := w.turns a, halted := w.halted,
    voiced := w.voiced a }

/-- Agents among `candidates` whose view differs between two worlds; the differential
tests compare it with the Python's per-agent diff. -/
def changedAgents (w w' : World) (candidates : List Agent) : List Agent :=
  candidates.filter (fun a => view w a != view w' a)

end Scope
