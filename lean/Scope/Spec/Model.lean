/-!
# Mirror of `orbit_audit`'s scope check and op effects

Mirror definitions follow `src/orbit_audit/` at commit `b7b1eeb` line by line; each
carries a `-- mirrors:` header naming the Python it follows. Orbit behaviour the
Python relies on is assumed as stated in `lean/scope-map.md` (A1–A7).

Kept exact, because that is where bugs hide (`lean/scope-map.md`):
* `None` vs empty list (`Option (List _)`) for scope limits and message recipients;
* the truthiness test `(agent,) if agent else ()` in `ControlOp.agents`;
* op names as the Python strings.

Abstracted: payload fields no check or effect reads (`String`), message sequence
numbers, and checks on hidden data (tool schemas, edit text), which are an
argument `ok : Bool` of `applyRaw`.
-/

namespace Scope

abbrev Agent := String
abbrev MsgId := String
abbrev ChannelId := String

-- mirrors: boundaries.py:8-23
inductive Boundary where
  | generation | beforeActivation | toolCall | toolResult | betweenTurns
  deriving DecidableEq, Repr, Inhabited

/-- Orbit's `CommunicationMessage`, with the fields delivery and the scope check read. -/
structure Message where
  id : MsgId
  channel : ChannelId
  sender : Agent
  recipients : List Agent
  content : String
  deriving DecidableEq, Repr, Inhabited

/-- Orbit's `CommunicationChannel`, with `readers`/`writers` already resolved to
`members` when unset (`delivery.py:82-86`). -/
structure Channel where
  id : ChannelId
  readers : List Agent
  writers : List Agent
  deriving DecidableEq, Repr, Inhabited

-- mirrors: ops.py:62-465, the 13 ops registered at ops.py:493-496.
-- `expected_revision` and `reason` are not modelled: the stale-revision check can only
-- add rejections and is part of `Hooks.extra` (Run.lean).
inductive Op where
  | replaceGeneration (agent : String) (content : String) (toolCalls : List String)
  | editGeneration (agent : String) (replacements : List (String × String))
  | forceToolCall (agent : String) (calls : List String)
  | stageInput (agent : String) (content : String)
  | blockToolCall (agent : String) (toolCallId : Option String) (message : String)
  | simulateToolResult (agent : String) (toolCallId : Option String) (result : String)
  | overrideToolResult (agent : String) (toolCallId : Option String) (result : String)
  | rewriteMessage (messageId : MsgId) (recipients : Option (List Agent)) (content : String)
  | dropMessage (messageId : MsgId) (recipients : Option (List Agent))
  | injectMessage (channel : ChannelId) (sender : Agent) (recipients : List Agent)
      (content : String) (overrideTopology : Bool)
  | halt
  | amendPending (steps : List (List Agent)) (scheduleRevision : Nat)
  | rollback (toRevision : Nat) (interventions : List Op)
  deriving Repr, Inhabited

namespace Op

-- mirrors: the `name` ClassVar of each op class, ops.py:117-446
def name : Op → String
  | .replaceGeneration .. => "replace_generation"
  | .editGeneration .. => "edit_generation"
  | .forceToolCall .. => "force_tool_call"
  | .stageInput .. => "stage_input"
  | .blockToolCall .. => "block_tool_call"
  | .simulateToolResult .. => "simulate_tool_result"
  | .overrideToolResult .. => "override_tool_result"
  | .rewriteMessage .. => "rewrite_message"
  | .dropMessage .. => "drop_message"
  | .injectMessage .. => "inject_message"
  | .halt => "halt"
  | .amendPending .. => "amend_pending"
  | .rollback .. => "rollback"

/-- The op families of ops.py:10-22. -/
inductive Family where
  | generation | activation | tool | message | turn | rollback
  deriving DecidableEq, Repr

def family : Op → Family
  | .replaceGeneration .. | .editGeneration .. | .forceToolCall .. => .generation
  | .stageInput .. => .activation
  | .blockToolCall .. | .simulateToolResult .. | .overrideToolResult .. => .tool
  | .rewriteMessage .. | .dropMessage .. | .injectMessage .. => .message
  | .halt | .amendPending .. => .turn
  | .rollback .. => .rollback

def allBoundaries : List Boundary :=
  [.generation, .beforeActivation, .toolCall, .toolResult, .betweenTurns]

-- mirrors: the `boundaries` ClassVar, ops.py:95, 189, 233, 242, 251, 260, 392, 447
def boundaries : Op → List Boundary
  | .replaceGeneration .. | .editGeneration .. | .forceToolCall .. => [.generation]
  | .stageInput .. => [.beforeActivation]
  | .blockToolCall .. | .simulateToolResult .. => [.toolCall]
  | .overrideToolResult .. => [.toolResult]
  | .rewriteMessage .. | .dropMessage .. | .injectMessage .. => allBoundaries
  | .halt | .amendPending .. => [.betweenTurns]
  | .rollback .. => allBoundaries

/-- The `agent` field, for the ops that have one. -/
def agentField? : Op → Option String
  | .replaceGeneration a .. | .editGeneration a .. | .forceToolCall a .. | .stageInput a ..
  | .blockToolCall a .. | .simulateToolResult a .. | .overrideToolResult a .. => some a
  | _ => none

-- mirrors: `getattr(op, "tool_call_id", None)`, controller.py:546
def toolCallId? : Op → Option String
  | .blockToolCall _ t _ | .simulateToolResult _ t _ | .overrideToolResult _ t _ => t
  | _ => none

/-- `(agent,) if agent else ()`: Python truthiness, so an empty name gives `[]`. -/
def agentTuple (agent : String) : List Agent :=
  if agent.isEmpty then [] else [agent]

mutual
  -- mirrors: ControlOp.agents ops.py:75-78; _ExistingMessageOp.agents ops.py:272-273;
  -- InjectMessage.agents ops.py:357-358; AmendPending.agents ops.py:416-417;
  -- Rollback.agents ops.py:460-461. Python sorts and dedupes; only membership matters here.
  def agents : Op → List Agent
    | .replaceGeneration a .. | .editGeneration a .. | .forceToolCall a .. | .stageInput a ..
    | .blockToolCall a .. | .simulateToolResult a .. | .overrideToolResult a .. => agentTuple a
    | .rewriteMessage _ rs _ | .dropMessage _ rs => rs.getD []
    | .injectMessage _ s rs _ _ => s :: rs
    | .halt => []
    | .amendPending steps _ => steps.flatten
    | .rollback _ ivs => agentsList ivs
  def agentsList : List Op → List Agent
    | [] => []
    | op :: rest => agents op ++ agentsList rest
end

end Op

/-- Delivery overlay, `delivery.py:29-35`. Rewrites are kept newest first, so the first
match is the current value of the Python dict entry. -/
structure Overlay where
  rewrites : List ((MsgId × Option Agent) × String) := []
  drops : List (MsgId × Option Agent) := []
  injections : List Message := []
  delivered : List (Agent × MsgId) := []
  deriving DecidableEq, Repr, Inhabited

/-- Orbit's schedule runtime, as far as `amend_pending` reads and writes it (A3). -/
structure Schedule where
  revision : Nat
  pending : List (List Agent)
  deriving DecidableEq, Repr, Inhabited

/-- The state an op can change or a check can read. `own a` lists (by name) the ops that wrote
`a`'s generation, staged input into `a`'s context, or decided or replaced one of `a`'s
tool calls or results: what `a` itself sees of the auditor apart from deliveries. -/
structure World where
  agents : List Agent
  channels : List Channel
  routes : Option (List (Agent × Agent))
  messages : List Message
  overlay : Overlay := {}
  schedule : Option Schedule := none
  halted : Bool := false
  own : Agent → List String := fun _ => []

namespace World

-- mirrors: `world.messages.get(id)`, ops.py:284, 292, 319, where `world.messages` is
-- `{m.id: m for m in state.messages}` (delivery.py:77): with a repeated id the last wins.
def findMsg (w : World) (id : MsgId) : Option Message :=
  w.messages.reverse.find? (·.id == id)

def findChannel (w : World) (id : ChannelId) : Option Channel :=
  w.channels.find? (·.id == id)

-- mirrors: CommunicationWorld.route_allowed, delivery.py:88-89
def routeAllowed (w : World) (sender recipient : Agent) : Bool :=
  match w.routes with
  | none => true
  | some edges => edges.contains (sender, recipient)

-- mirrors: DeliveryOverlay.delivered_to, delivery.py:60-61
def deliveredTo (w : World) (id : MsgId) : List Agent :=
  (w.overlay.delivered.filter (·.2 == id)).map (·.1)

end World

-- mirrors: recipients_or_all, delivery.py:99-100
def recipientsOrAll (m : Message) (recipients : Option (List Agent)) : List Agent :=
  recipients.getD m.recipients

namespace Op

-- mirrors: ControlOp.affected ops.py:80-84; _ExistingMessageOp.affected ops.py:275-287;
-- RewriteMessage.affected ops.py:317-321; AmendPending.affected ops.py:419-423. The world
-- is always given: the controller passes `CommunicationWorld.current()` and the schedule's
-- pending steps (`AuditController._pending`), which here are `w.schedule`.
def affected (w : World) : Op → List Agent
  | .rewriteMessage id rs c =>
    match w.findMsg id with
    | none => (Op.rewriteMessage id rs c).agents
    | some m => ((recipientsOrAll m rs).filter (· != m.sender)) ++ [m.sender]
  | .dropMessage id rs =>
    match w.findMsg id with
    | none => (Op.dropMessage id rs).agents
    | some m => (recipientsOrAll m rs).filter (· != m.sender)
  | .amendPending steps rev =>
    (Op.amendPending steps rev).agents ++
      (match w.schedule with
       | none => []
       | some s => s.pending.flatten)
  | op => op.agents

end Op

-- mirrors: AuditScope, scope.py:25-35
structure AuditScope where
  ops : Option (List String)
  agents : Option (List Agent)
  deriving DecidableEq, Repr, Inhabited

/-- Why an op is outside a scope (scope.py:53-75); the text is not modelled. -/
inductive ScopeProblem where
  | opOutside (name : String)
  | agentsOutside (name : String) (outside : List Agent)
  | intervention (inner : ScopeProblem)
  deriving DecidableEq, Repr

namespace AuditScope

-- mirrors: AuditScope.allows_op, scope.py:50-51
def allowsOp (s : AuditScope) (name : String) : Bool :=
  match s.ops with
  | none => true
  | some names => names.contains name

def allowsAgent (s : AuditScope) (a : Agent) : Bool :=
  match s.agents with
  | none => true
  | some names => names.contains a

/-- The checks of scope.py:62-69, before the rollback recursion. -/
def ownProblem (s : AuditScope) (w : World) (op : Op) : Option ScopeProblem :=
  if !s.allowsOp op.name then some (.opOutside op.name)
  else
    let outside := (op.affected w).filter (fun a => !s.allowsAgent a)
    if !outside.isEmpty then some (.agentsOutside op.name outside) else none

mutual
  -- mirrors: AuditScope.problem, scope.py:53-75
  def problem (s : AuditScope) (w : World) : Op → Option ScopeProblem
    | op@(.rollback _ ivs) =>
      match ownProblem s w op with
      | some p => some p
      | none => (problemList s w ivs).map .intervention
    | op => ownProblem s w op
  /-- The first intervention's problem, in order (scope.py:70-74). -/
  def problemList (s : AuditScope) (w : World) : List Op → Option ScopeProblem
    | [] => none
    | op :: rest =>
      match problem s w op with
      | some p => some p
      | none => problemList s w rest
end

end AuditScope

/-- The boundary an op is applied at: `BoundaryView` fields the controller reads
(policy.py:30-60). -/
structure Ctx where
  boundary : Boundary
  revision : Nat
  agent : Option Agent
  toolCallId : Option String := none
  deriving DecidableEq, Repr, Inhabited

/-- Why `applyRaw` refused an op: an `OpError`, or Orbit's `ValueError` for a schedule
amendment. -/
inductive ApplyError where
  | noBoundaryAgent | needsContent | hidden | noMessage | notRecipients | alreadyDelivered
  | noChannel | toOwnSender | offTopology | noSchedule | staleSchedule | invalidStep
  | unknownAgents | notAnEffect
  deriving DecidableEq, Repr

/-- Effect of an op that changes the boundary agent's own generation, context or tool
call: the controller acts on the boundary's agent (`request`, `activation`, `call`),
never on `op.agent` (controller.py:201-280). -/
def touchOwn (ctx : Ctx) (op : Op) (w : World) : Except ApplyError World :=
  match ctx.agent with
  | none => .error .noBoundaryAgent
  | some b => .ok { w with own := fun a => if a == b then w.own a ++ [op.name] else w.own a }

-- mirrors: _ExistingMessageOp._target, ops.py:289-307. Returns the message and the
-- overlay keys; `None` recipient means every recipient.
def target (w : World) (id : MsgId) (recipients : Option (List Agent)) :
    Except ApplyError (Message × List (MsgId × Option Agent)) :=
  match w.findMsg id with
  | none => .error .noMessage
  | some m =>
    let targets := recipientsOrAll m recipients
    if targets.any (fun r => !m.recipients.contains r) then .error .notRecipients
    else if targets.any (fun r => (w.deliveredTo id).contains r) then .error .alreadyDelivered
    else
      let keys := match recipients with
        | none => [(id, none)]
        | some _ => targets.map (fun r => (id, some r))
      .ok (m, keys)

/-- Id of a new injected message; Orbit's ids are fresh hex, never a canonical id (A2). -/
def injectedId (w : World) : MsgId :=
  "injected-" ++ toString w.overlay.injections.length

/-- One op's effect at boundary `ctx`, assuming every check before it passed.
`ok` stands for apply-time checks on data the model does not hold (tool schemas,
whether an edit's text occurs); `false` means the op raises `OpError` there.
Rollback is not an effect: the run handles it (Run.lean). -/
def applyRaw (ctx : Ctx) (ok : Bool) : Op → World → Except ApplyError World
  -- mirrors: ReplaceGeneration.produce ops.py:122-131, applied at controller.py:218-233
  | op@(.replaceGeneration _ content calls), w =>
    if content.isEmpty && calls.isEmpty then .error .needsContent
    else if !ok then .error .hidden
    else touchOwn ctx op w
  -- mirrors: EditGeneration.produce ops.py:153-167; ForceToolCall.produce ops.py:177-184.
  -- On failure the agent gets the model's own output (controller.py:221-226): no effect.
  | op@(.editGeneration ..), w | op@(.forceToolCall ..), w =>
    if !ok then .error .hidden else touchOwn ctx op w
  -- mirrors: StageInput.apply ops.py:213-218, applied at controller.py:239-245
  | op@(.stageInput ..), w => touchOwn ctx op w
  -- mirrors: controller.py:255-262 (block, simulate) and 270-276 (override)
  | op@(.blockToolCall ..), w | op@(.simulateToolResult ..), w
  | op@(.overrideToolResult ..), w => touchOwn ctx op w
  -- mirrors: RewriteMessage.apply ops.py:323-328
  | .rewriteMessage id rs content, w =>
    match target w id rs with
    | .error e => .error e
    | .ok (_, keys) =>
      .ok { w with overlay := { w.overlay with
        rewrites := keys.map (fun k => (k, content)) ++ w.overlay.rewrites } }
  -- mirrors: DropMessage.apply ops.py:336-339
  | .dropMessage id rs, w =>
    match target w id rs with
    | .error e => .error e
    | .ok (_, keys) => .ok { w with overlay := { w.overlay with drops := keys ++ w.overlay.drops } }
  -- mirrors: InjectMessage.apply ops.py:360-387
  | .injectMessage channel sender recipients content override, w =>
    match w.findChannel channel with
    | none => .error .noChannel
    | some c =>
      if recipients.contains sender then .error .toOwnSender
      else
        let violation := !c.writers.contains sender ||
          recipients.any (fun r => !c.readers.contains r || !w.routeAllowed sender r)
        if violation && !override then .error .offTopology
        else
          let m : Message := { id := injectedId w, channel := channel, sender := sender,
                               recipients := recipients, content := content }
          .ok { w with overlay := { w.overlay with injections := w.overlay.injections ++ [m] } }
  -- mirrors: Halt.apply ops.py:403-404 (Orbit `runtime.halt`)
  | .halt, w => .ok { w with halted := true }
  -- mirrors: AmendPending.apply ops.py:425-434 and Orbit ScheduleRuntime.amend_pending
  -- (execution/plans.py:255-267): `_check_amendment` (220-230), then each step's
  -- `distinct_agents` (configs/scheduling.py:44-51; `not name.strip()`, read as "all
  -- whitespace" with Lean's `Char.isWhitespace`), then `_validate_steps` (134-140).
  -- `ok = false` stands for the checks on data the model does not hold: a blank reason
  -- (ValueError), and a non-quiescent or terminated schedule (RuntimeError, which the
  -- controller does not catch: the branch errors, so no later op is applied).
  | .amendPending steps rev, w =>
    match w.schedule with
    | none => .error .noSchedule
    | some s =>
      if rev != s.revision then .error .staleSchedule
      else if !ok then .error .hidden
      else if steps.any (fun st => st.isEmpty || st.any (fun a => a.toList.all Char.isWhitespace)
          || st.eraseDups.length != st.length) then .error .invalidStep
      else if steps.flatten.any (fun a => !w.agents.contains a) then .error .unknownAgents
      else .ok { w with schedule := some { revision := s.revision + 1, pending := steps } }
  | .rollback .., _ => .error .notAnEffect

end Scope
