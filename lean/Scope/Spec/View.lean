import Scope.Spec.Model

/-!
# What each agent sees (specification)

`view w a` is a specification definition: it has no Python counterpart and is defined
from the world alone, never from `Op.affected`. It collects what an op can directly
change for agent `a`, under the decisions recorded in `lean/scope-map.md`, except the
shared id and sequence counters and `schedule.revision`, which the model does not hold
per agent:

* D2 (a): deliveries are what the delivery overlay changes. A post the agent's model
  makes is that agent's own output; its spread is for the later information-flow work.
* D3: an agent's pending turns, with their positions, are part of its view.
* `halted` is shared by every agent: halting ends everyone's remaining turns (D1).
* `rewrittenAs`, `injectedAs`: words the overlay attributes to `a` that `a` never wrote:
  the current rewrite under each key on one of `a`'s messages, and injections sent as `a`. This is the "sender by attribution" of
  ops.py:317-321, 357-358. Withholding `a`'s message is not in it: it puts no words in
  `a`'s mouth, and `drop_message` does not count the sender (ops.py:331-339).

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

/-- What `a` is shown for canonical message `m` (delivery.py:40-49): its own messages as
sent; otherwise nothing if dropped, else the rewrite for `a` if any. -/
def shownOne (w : World) (a : Agent) (m : Message) : Option Message :=
  if m.sender == a then some m
  else if w.dropped m.id a then none
  else match w.rewriteFor m.id a with
    | some c => some { m with content := c }
    | none => some m

/-- Every channel message `a` is shown, after the overlay. -/
def inbox (w : World) (a : Agent) : List Message :=
  (w.selected a).filterMap (w.shownOne a)
    ++ w.overlay.injections.filter (fun m => w.canRead a m.channel && m.recipients.contains a)

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

/-- The current rewrite stored under an overlay key (the Python dict's value). -/
def rewriteAt (w : World) (k : MsgId × Option Agent) : Option String :=
  (w.overlay.rewrites.find? (·.1 == k)).map (·.2)

/-- Rewrites attributed to `a`: for each overlay key on one of `a`'s messages, the text
stored there. -/
def rewrittenAs (w : World) (a : Agent) (k : MsgId × Option Agent) : Option String :=
  if (w.findMsg k.1).any (·.sender == a) then w.rewriteAt k else none

/-- Injections sent as `a`. -/
def injectedAs (w : World) (a : Agent) : List Message :=
  w.overlay.injections.filter (·.sender == a)

end World

structure AgentView where
  own : List String
  inbox : List Message
  turns : List (Nat × Nat)
  halted : Bool
  rewrittenAs : MsgId × Option Agent → Option String
  injectedAs : List Message

def view (w : World) (a : Agent) : AgentView :=
  { own := w.own a, inbox := w.inbox a, turns := w.turns a, halted := w.halted,
    rewrittenAs := w.rewrittenAs a, injectedAs := w.injectedAs a }

/-- Agents among `candidates` whose view differs between two worlds; the differential
tests compare it with the Python's per-agent diff. `rewrittenAs` can only differ at keys
stored in one of the two overlays, so comparing it there decides it. -/
def changedAgents (w w' : World) (candidates : List Agent) : List Agent :=
  let keys := (w.overlay.rewrites ++ w'.overlay.rewrites).map (·.1)
  candidates.filter fun a =>
    w.own a != w'.own a || w.inbox a != w'.inbox a || w.turns a != w'.turns a
      || w.halted != w'.halted || w.injectedAs a != w'.injectedAs a
      || keys.any (fun k => w.rewrittenAs a k != w'.rewrittenAs a k)

end Scope
