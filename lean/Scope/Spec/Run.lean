import Scope.Spec.Model

/-!
# Mirror of the controller's decision path and the branch loop

`decide` mirrors `AuditController._decide` (controller.py:412-458) with `_problem`
(524-541), `_rollback` (460-476) and `_rollback_problem` (479-501). `runBranch` and
`runAudit` mirror one branch of `audit_solver` and its fork loop (runner.py:99-147).

The policy, the LLMs and Orbit's own steps between boundaries are arguments, so every
theorem about a run holds for all of them. A function of (branch, revision, world) is
enough: in any one run, a stateful policy's output at each boundary is such a function.
Replay is modelled without its fingerprint check (controller.py:374-379), which only
ends branches: the model's runs include every run the Python can make.
-/

namespace Scope

-- mirrors: the `family` argument each boundary method passes to `_decide`
-- (controller.py:211, 236, 251, 270, 288)
def familyAt : Boundary → Op.Family
  | .generation => .generation
  | .beforeActivation => .activation
  | .toolCall | .toolResult => .tool
  | .betweenTurns => .turn

-- mirrors: _SOLE, controller.py:93-97
def soleBoundary : Boundary → Bool
  | .generation | .toolCall | .toolResult => true
  | _ => false

inductive Problem where
  | scope (p : ScopeProblem)
  | boundary | unknownAgents | targeted | pinned | sole
  | extra (reason : String)
  | rollbackInReplay | rollbackNotAlone | rollForward | interventionBoundary
  /-- A payload pydantic refuses before the op exists: an empty `agent`, `to_revision < 1` (ops.py:440) or a
  rollback inside a rollback (ops.py:447-448). -/
  | invalid
  deriving DecidableEq, Repr

-- mirrors: AuditController._problem, controller.py:524-544. The stale-revision check
-- (530-531) is part of `Hooks.extra`. `scope = none` means no scope check (526).
-- An op with an empty `agent` cannot be built (`Field(min_length=1)`, ops.py:95, 189, 221):
-- the model rejects it as an invalid payload, ahead of every check.
def checkProblem (scope : Option AuditScope) (ctx : Ctx) (op : Op) (w : World) :
    Option Problem :=
  if op.agentField? == some "" then some .invalid else
  match scope.bind (fun s => s.problem w op) with
  | some p => some (.scope p)
  | none =>
    if !op.boundaries.contains ctx.boundary
        || !(op.family == familyAt ctx.boundary || op.family == .message) then
      some .boundary
    else if !w.agents.isEmpty && op.agents.any (fun a => !w.agents.contains a) then
      some .unknownAgents
    -- Python compares the sorted, deduplicated tuple with `(view.agent,)`: an op that
    -- names no agent, or another agent, is refused at an agent's boundary.
    else if op.family != .message && ctx.agent.isSome
        && (op.agents.isEmpty || !op.agents.all (· == ctx.agent.getD "")) then
      some .targeted
    else
      match op.toolCallId? with
      | some t => if some t != ctx.toolCallId then some .pinned else none
      | none => none

/-- Checks the model does not mirror. Each can only add rejections. -/
structure Hooks where
  /-- Batch-level: stale `expected_revision`, the intervention budget, the approver's
  veto (controller.py:442-449, 530-531); for a rollback also branch and intervention
  budgets, defences and `fork_problem` (490-501). -/
  extra : Ctx → List Op → World → Option String
  /-- Apply-time checks on data the model does not hold (`applyRaw`'s `ok`). -/
  applyOk : Ctx → Op → World → Bool

def Op.isRollback : Op → Bool
  | .rollback .. => true
  | _ => false

-- mirrors: AuditController._rollback_problem, controller.py:479-501.
-- `boundaryAt t` is the boundary recorded at revision `t` of this branch.
def rollbackProblem (scope : Option AuditScope) (hooks : Hooks) (ctx : Ctx)
    (boundaryAt : Nat → Option Boundary) (op : Op) (w : World) : Option Problem :=
  match scope.bind (fun s => s.problem w op) with
  | some p => some (.scope p)
  | none =>
    match op with
    | .rollback t ivs =>
      if t == 0 || ivs.any Op.isRollback then some .invalid
      else if t > ctx.revision then some .rollForward
      else
        match boundaryAt t with
        | none => some .rollForward
        | some b =>
          if ivs.any (fun iv => !iv.boundaries.contains b) then some .interventionBoundary
          else (hooks.extra ctx [op] w).map .extra
    | _ => none

inductive Source where
  | live | replay | fork
  deriving DecidableEq, Repr

/-- One applied op: what the controller records with outcome "applied". -/
structure Entry where
  branch : Nat
  revision : Nat
  source : Source
  scope : Option AuditScope
  ctx : Ctx
  op : Op
  pre : World
  post : World

/-- Apply a checked batch's ops one by one. An op whose effect fails is rejected alone and
changes nothing (controller.py:218-223, 239-241, 291-294, 519-521). -/
def applyOps (mk : Op → World → World → Entry) (ctx : Ctx) (hooks : Hooks) :
    List Op → World → World × List Entry
  | [], w => (w, [])
  | op :: rest, w =>
    match applyRaw ctx (hooks.applyOk ctx op w) op w with
    | .error _ => applyOps mk ctx hooks rest w
    | .ok w' =>
      let r := applyOps mk ctx hooks rest w'
      (r.1, mk op w w' :: r.2)

inductive Outcome where
  | next (w : World) (entries : List Entry)
  | rollback (entries : List Entry) (toRevision : Nat) (interventions : List Op)

-- mirrors: AuditController._decide, controller.py:412-458, and the boundary method that
-- applies the returned family ops. Message ops go first (455-457), then the family's.
def decide (scope : Option AuditScope) (hooks : Hooks) (mk : Op → World → World → Entry)
    (ctx : Ctx) (replaying : Bool) (boundaryAt : Nat → Option Boundary) (ops : List Op)
    (w : World) : Outcome :=
  if ops.any Op.isRollback then
    -- mirrors: AuditController._rollback, controller.py:460-476
    if replaying then .next w []
    else
      match ops with
      | [op] =>
        match rollbackProblem scope hooks ctx boundaryAt op w with
        | some _ => .next w []
        | none =>
          match op with
          | .rollback t ivs => .rollback [mk op w w] t ivs
          | _ => .next w []
      | _ => .next w []
  else
    let checked := ops.any (fun op => (checkProblem scope ctx op w).isSome)
    let producers := ops.filter (fun op => op.family == familyAt ctx.boundary)
    let sole := producers.length > 1 && soleBoundary ctx.boundary
    if checked || sole || (hooks.extra ctx ops w).isSome then .next w []
    else
      let ordered := ops.filter (fun op => op.family == .message)
        ++ ops.filter (fun op => op.family != .message)
      let r := applyOps mk ctx hooks ordered w
      .next r.1 r.2

/-- A branch's prefix to replay, then the rollback's interventions at `fork`
(branches.py `Replay`, runner.py:135-140). `decisions` holds the parent's batches at
revisions `1 … fork`. -/
structure Replay where
  decisions : List (List Op)
  fork : Nat
  interventions : List Op

/-- Everything outside the controller, as arguments. -/
structure Env where
  /-- The boundary reached at a revision of a branch. -/
  ctx : Nat → Nat → World → Ctx
  /-- Orbit, the agents' models and tools between two boundaries. -/
  step : Nat → Nat → World → World
  /-- Whether the branch ends before this revision (run complete, limits). -/
  stop : Nat → Nat → World → Bool
  hooks : Nat → Hooks

abbrev Policy := Nat → Nat → World → List Op

structure BranchOut where
  entries : List Entry
  decisions : List (List Op)
  rollback : Option (Nat × List Op)

def sourceAt (replay : Option Replay) (rev : Nat) : Source :=
  match replay with
  | none => .live
  | some r => if rev < r.fork then .replay else if rev == r.fork then .fork else .live

-- mirrors: the batch `_decide` takes, controller.py:419-429
def batchAt (policy : Policy) (b : Nat) (replay : Option Replay) (rev : Nat) (w : World) :
    List Op :=
  match replay with
  | none => policy b rev w
  | some r =>
    if rev < r.fork then r.decisions.getD (rev - 1) []
    else if rev == r.fork then r.interventions ++ policy b rev w
    else policy b rev w

/-- One branch, from revision `rev` with `fuel` boundaries left. -/
def runBranch (scope : Option AuditScope) (env : Env) (policy : Policy) (b : Nat)
    (replay : Option Replay) :
    Nat → Nat → World → List (List Op) → List Boundary → List Entry → BranchOut
  | 0, _, _, decs, _, es => { entries := es, decisions := decs, rollback := none }
  | fuel + 1, rev, w, decs, bounds, es =>
    if env.stop b rev w then { entries := es, decisions := decs, rollback := none }
    else
      let ctx := { env.ctx b rev w with revision := rev }
      let bounds' := bounds ++ [ctx.boundary]
      let ops := batchAt policy b replay rev w
      let replaying := (sourceAt replay rev) == .replay
      let mk := fun op pre post =>
        ({ branch := b, revision := rev, source := sourceAt replay rev, scope := scope,
           ctx := ctx, op := op, pre := pre, post := post } : Entry)
      match decide scope (env.hooks b) mk ctx replaying (fun t => bounds'[t - 1]?) ops w with
      | .next w' es' =>
        runBranch scope env policy b replay fuel (rev + 1) (env.step b rev w') (decs ++ [ops])
          bounds' (es ++ es')
      | .rollback es' t ivs =>
        { entries := es ++ es', decisions := decs ++ [ops], rollback := some (t, ivs) }

/-- Branches in order; a rollback queues a child that replays its parent's prefix. -/
def auditLoop (scope : Option AuditScope) (env : Env) (policy : Policy) (w0 : World)
    (fuel : Nat) : Nat → Nat → List (Option Replay) → List Entry → List Entry
  | 0, _, _, acc => acc
  | _ + 1, _, [], acc => acc
  | n + 1, b, r :: rest, acc =>
    let out := runBranch scope env policy b r fuel 1 w0 [] [] []
    let child := out.rollback.map fun (t, ivs) =>
      some ({ decisions := out.decisions.take t, fork := t, interventions := ivs } : Replay)
    auditLoop scope env policy w0 fuel n (b + 1) (rest ++ child.toList) (acc ++ out.entries)

/-- Every op applied in an audit of at most `maxBranches` branches of at most `fuel`
boundaries each, starting every branch from `w0` (runner.py:104-127). -/
def runAudit (scope : Option AuditScope) (env : Env) (policy : Policy) (w0 : World)
    (maxBranches fuel : Nat) : List Entry :=
  auditLoop scope env policy w0 fuel maxBranches 0 [none] []

end Scope
