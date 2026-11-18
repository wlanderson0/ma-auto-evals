import Scope.Spec.Run

/-!
# Mirror of rollback: replay, the fork and the branch chain (rollback plan step 2)

Mirror definitions follow `src/orbit_audit/` at commit `99024cb`; `lean/rollback-map.md`
maps the code. Each boundary's batch goes through `decide` (`Run.lean`, the controller's
`_decide`) unchanged, so the scope results carry over to this model directly. What this
file adds, and `Run.lean` leaves out (rollback-map M1, M2):

* a branch's state holds the id provider (`BranchIds`) and per-branch state the scope
  model does not see (`hidden`: agents' conversations, store models, counters);
* state shared across branches (`Shared`: the budget ledger, the approver, the policy's
  memory) is threaded through the whole audit;
* a child replays its parent's recorded steps: the recorded batch (emptied when the
  approver vetoed it, M1), the recorded `over_budget` verdict and the recorded model output;
* the fingerprint check comes before any op, and `diverge` at the fork comes after it;
* branches form a chain (M3), and a child whose run ends before its fork is a desync (RB2).

Nondeterminism stays an argument: `Env.gen`, `Env.step` and `Env.done` take the branch
index, so two branches may differ anywhere. T8 (Orbit is deterministic given the outputs
served) is a hypothesis of the statements that need it, never part of the types.

Not modelled: the number of model calls within one boundary (RB5); effectful tools (a fork
past one is refused, R5); ids minted by ops (`ReplaceGeneration`, `ForceToolCall`,
`StageInput`, `InjectMessage`; an injected message's id here comes from the world,
`injectedId`); a policy that raises instead of returning ops (RB6: assumption A7 is a
precondition of every statement).

Every `Env` reader takes the whole `State`, ids included, though in the Python only ids
already minted reach a view or the fingerprint. Statements that depend on this say so; the
witnesses mint ids through `step` and fingerprint only what was minted.
-/

namespace Scope.Branch

/-- A model output, abstract. -/
abbrev Output := Nat

/-- State shared across branches, abstract: everything `Env` reads or updates with it. -/
abbrev Shared := Nat

-- mirrors: BranchIds, branches.py:115-136 (`tag` is the Python's prefix).
structure Ids where
  tag : Nat := 0
  count : Nat := 0
  deriving DecidableEq, Repr

-- mirrors: BranchIds.diverge, branches.py:131-132
def Ids.diverge (ids : Ids) (b : Nat) : Ids := { ids with tag := b }

/-- A branch's state: `S` of `lean/rollback-map.md` (T8). -/
structure State where
  world : World
  ids : Ids := {}
  hidden : Nat := 0

-- mirrors: BranchStatus, branches.py:53-60
inductive Status where
  | completed | rolledBack | replayDesync | budgetExhausted | error
  deriving DecidableEq, Repr

/-- How a run is cut short by something outside Orbit's own run: a spent budget or an
Inspect limit, or an error (runner.py:167-175). -/
inductive Cut where
  | limit | error
  deriving DecidableEq, Repr

def Cut.status : Cut → Status
  | .limit => .budgetExhausted
  | .error => .error

-- mirrors: Step, branches.py:141-157, with the boundary the controller records next to it
-- (`controller.steps`, read by `_rollback_problem`, controller.py:491-493).
structure Step where
  boundary : Boundary
  fingerprint : Nat
  decisions : List Op
  overBudget : Bool
  output : Output

-- mirrors: Replay, branches.py:160-176. `steps` are the parent's steps at revisions
-- `1 … fork` (runner.py:138).
structure Replay where
  steps : List Step
  fork : Nat
  interventions : List Op

/-- Everything outside the controller, as arguments. -/
structure Env where
  /-- The boundary reached, built from the state (`BoundaryView`, controller.py:366-376). -/
  ctx : State → Ctx
  /-- What `fingerprint(view)` hashes (branches.py:178-196); collisions are allowed (T7 is
  not assumed). -/
  fingerprint : State → Nat
  /-- The live model output at a boundary of branch `b`. -/
  gen : Nat → Nat → State → Output
  /-- Orbit, the agents and pure tools between two boundaries of branch `b`, given the
  output the boundary served. -/
  step : Nat → Nat → Output → State → State
  /-- Orbit's run is complete before this boundary of branch `b`. -/
  done : Nat → Nat → State → Bool
  /-- The run is cut short before this boundary: budgets and Inspect limits are cumulative
  across branches, so this reads the shared state. -/
  cut : Nat → Nat → Shared → State → Option Cut
  /-- The stale `expected_revision` check (controller.py:534-535). -/
  stale : Ctx → List Op → World → Option String
  /-- Apply-time checks on data the model does not hold (`Hooks.applyOk`), given the
  boundary's model output: whether an edit's text occurs in it (ops.py:153-184). -/
  applyOk : Output → Ctx → Op → World → Bool
  /-- `budget.intervention_problem` (controller.py:517). In the Python a batch with no
  counted op always fits; here `overBudget` is arbitrary, which only adds runs. -/
  overBudget : Shared → Ctx → List Op → Bool
  /-- The approver's veto (controller.py:551-562). -/
  veto : Shared → Ctx → List Op → State → Bool
  /-- A rollback's checks beyond `rollbackProblem`: its stale revision (controller.py:487-488),
  the branch and intervention budgets, defences and `fork_problem` (496-504). -/
  rollbackExtra : Nat → Shared → Ctx → List Op → World → Option String
  /-- Everything the shared state records after a boundary: the policy's `notice`, the
  budget's counts (controller.py:577-594). -/
  notice : Shared → Nat → Nat → List Op → List Entry → Shared
  /-- What the shared state records when branch `b` starts: `budget.branches`, the policy's
  `bind` and `start` (runner.py:117, controller.py:169, 196). -/
  begin : Shared → Nat → Shared

/-- What the policy returns at a boundary of branch `b`: it may read the shared state. -/
abbrev Policy := Nat → Nat → Shared → State → List Op

/-- One branch in progress: the next revision, the shared and branch state, what the
controller recorded, and how the branch ended once it has. -/
structure Cfg where
  rev : Nat := 1
  shared : Shared
  st : State
  steps : List Step := []
  entries : List Entry := []
  ended : Option Status := none
  rollback : Option (Nat × List Op) := none

def recordedAt (replay : Option Replay) (rev : Nat) : Option Step :=
  replay.bind (fun r => r.steps[rev - 1]?)

-- mirrors: AuditController.replaying, controller.py:353-355
def replayingAt (replay : Option Replay) (rev : Nat) : Bool :=
  match replay with
  | none => false
  | some r => rev < r.fork

def forkAt (replay : Option Replay) (rev : Nat) : Bool :=
  match replay with
  | none => false
  | some r => rev == r.fork

def sourceOf (replay : Option Replay) (rev : Nat) : Source :=
  if replayingAt replay rev then .replay else if forkAt replay rev then .fork else .live

-- mirrors: the fingerprint check of `_view`, controller.py:377-383, with `Replay.step`
-- raising for a revision past the recording (branches.py:170-175)
def desynced (env : Env) (replay : Option Replay) (rev : Nat) (st : State) : Bool :=
  match replay with
  | none => false
  | some r =>
    if rev ≤ r.fork then
      match r.steps[rev - 1]? with
      | none => true
      | some s => s.fingerprint != env.fingerprint st
    else false

-- mirrors: the end of `_run_branch`, runner.py:167-182: a run that ends on its own before
-- boundary `rev` reached `rev - 1` boundaries, and a child that did not reach its fork
-- desynced (RB2).
def finished (replay : Option Replay) (rev : Nat) : Status :=
  match replay with
  | some r => if rev - 1 < r.fork then .replayDesync else .completed
  | none => .completed

-- mirrors: the batch `_decide` takes, controller.py:423-433
def batchAt (policy : Policy) (b : Nat) (replay : Option Replay) (rev : Nat) (sh : Shared)
    (st : State) : List Op :=
  match replay with
  | none => policy b rev sh st
  | some r =>
    if rev < r.fork then ((r.steps[rev - 1]?).map (·.decisions)).getD []
    else if rev == r.fork then r.interventions ++ policy b rev sh st
    else policy b rev sh st

/-- One boundary of branch `b` (controller.py:366-462, then Orbit to the next boundary).
`st0` is the state the view is built from; `diverge` changes only the ids minted from
here on (controller.py:384-385). -/
def tick (scope : Option AuditScope) (env : Env) (policy : Policy) (b : Nat)
    (replay : Option Replay) (c : Cfg) : Cfg :=
  match c.ended with
  | some _ => c
  | none =>
    let rev := c.rev
    let st0 := c.st
    -- a limit or an error is classified before a normal end (runner.py:167-182)
    match env.cut b rev c.shared st0 with
    | some x => { c with ended := some x.status }
    | none =>
    if env.done b rev st0 then { c with ended := some (finished replay rev) }
    else
      if desynced env replay rev st0 then { c with ended := some .replayDesync }
      else
        let st := if forkAt replay rev then { st0 with ids := st0.ids.diverge b } else st0
        let ctx : Ctx := { env.ctx st0 with revision := rev }
        let ops := batchAt policy b replay rev c.shared st0
        let replaying := replayingAt replay rev
        -- mirrors: _over_budget, controller.py:506-518
        let over := if replaying then ((recordedAt replay rev).map (·.overBudget)).getD false
          else env.overBudget c.shared ctx ops
        let sole := (ops.filter (fun op => op.family == familyAt ctx.boundary)).length > 1
          && soleBoundary ctx.boundary
        let problems := ops.any (fun op => (checkProblem scope ctx op st.world).isSome)
          || sole || (env.stale ctx ops st.world).isSome
        -- mirrors: controller.py:448-453; the approver is not asked during replay (557)
        let vetoed := !ops.any Op.isRollback && !replaying && !problems && !over
          && env.veto c.shared ctx ops st0
        -- mirrors: _proceed, controller.py:388-414: the recorded output while replaying.
        -- The model's input is fixed before the boundary's ops, so it reads `st`.
        let out := if replaying then ((recordedAt replay rev).map (·.output)).getD 0
          else env.gen b rev st
        let hooks : Hooks :=
          { extra := fun ctx' ops' w' =>
              if ops'.any Op.isRollback then env.rollbackExtra b c.shared ctx' ops' w'
              else match env.stale ctx' ops' w' with
                | some r => some r
                | none => if over || vetoed then some "rejected" else none,
            applyOk := env.applyOk out }
        let mk := fun op pre post =>
          ({ branch := b, revision := rev, source := sourceOf replay rev, scope := scope,
             ctx := ctx, op := op, pre := pre, post := post } : Entry)
        let bAt := fun t => (c.steps.map (·.boundary) ++ [ctx.boundary])[t - 1]?
        let recorded := if vetoed then [] else ops
        let fp := env.fingerprint st0
        match decide scope hooks mk ctx replaying bAt ops st.world with
        | .rollback es t ivs =>
          { c with st := st, shared := env.notice c.shared b rev ops es,
                   steps := c.steps ++ [⟨ctx.boundary, fp, recorded, over, 0⟩],
                   entries := c.entries ++ es, ended := some .rolledBack,
                   rollback := some (t, ivs) }
        | .next w es =>
          let st1 := { st with world := w }
          { rev := rev + 1, shared := env.notice c.shared b rev ops es,
            st := env.step b rev out st1,
            steps := c.steps ++ [⟨ctx.boundary, fp, recorded, over, out⟩],
            entries := c.entries ++ es, ended := none, rollback := none }

def run (scope : Option AuditScope) (env : Env) (policy : Policy) (b : Nat)
    (replay : Option Replay) : Nat → Cfg → Cfg
  | 0, c => c
  | n + 1, c => run scope env policy b replay n (tick scope env policy b replay c)

/-- How branch `b` starts: the shared state records the start, and the branch state is the
sample's initial state with fresh ids. -/
def start (env : Env) (sh : Shared) (b : Nat) (s0 : State) : Cfg :=
  { shared := env.begin sh b, st := { s0 with ids := {} } }

/-- Branch `b` from the sample's initial state `s0`, with fresh ids (`BranchIds()`,
runner.py:118) and at most `fuel` boundaries (runner.py:152-182). Running out of fuel is
the model's stand-in for a limit. -/
def branch (scope : Option AuditScope) (env : Env) (policy : Policy) (b : Nat)
    (replay : Option Replay) (fuel : Nat) (sh : Shared) (s0 : State) : Cfg :=
  let c := run scope env policy b replay fuel (start env sh b s0)
  if c.ended.isNone then { c with ended := some .budgetExhausted } else c

-- mirrors: the branch loop of `solve`, runner.py:99-147: one branch at a time; a rollback
-- queues its one child, which replays the parent's steps up to the fork. The first
-- argument is fuel: the Python bounds branches through `max_branches`, which
-- `rollbackExtra` checks, so the model can also stop on a branch that rolled back.
def auditLoop (scope : Option AuditScope) (env : Env) (policy : Policy) (s0 : State)
    (fuel : Nat) : Nat → Nat → Option Replay → Shared → List Cfg → List Cfg
  | 0, _, _, _, acc => acc
  | n + 1, b, r, sh, acc =>
    let out := branch scope env policy b r fuel sh s0
    match out.rollback with
    | some (t, ivs) =>
      auditLoop scope env policy s0 fuel n (b + 1)
        (some { steps := out.steps.take t, fork := t, interventions := ivs }) out.shared
        (acc ++ [out])
    | none => acc ++ [out]

/-- Every branch of an audit, in order. -/
def runAudit (scope : Option AuditScope) (env : Env) (policy : Policy) (s0 : State)
    (sh : Shared) (maxBranches fuel : Nat) : List Cfg :=
  auditLoop scope env policy s0 fuel maxBranches 0 none sh []

-- mirrors: `_adopt(state, completed or outcome, ...)`, runner.py:144: with a chain, the
-- last branch.
def adopted (outs : List Cfg) : Option Cfg := outs.getLast?

-- mirrors: `scored_view`, experiments/scorers.py:114-129 (D5): the adopted branch is
-- scored only if it completed.
def scored (outs : List Cfg) : Option Cfg :=
  match adopted outs with
  | some o => if o.ended == some .completed then some o else none
  | none => none

end Scope.Branch
