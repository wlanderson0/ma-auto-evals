import Scope.Spec.Model
import Scope.Spec.View
import Scope.Spec.Run

/-!
# The statements (frozen)

Each property of the plan is a `Prop` here; `Scope/Proofs/*.lean` proves it as a
theorem of that type, so a proof can never weaken the statement it proves. This file and
the three it imports are the specification; CI records their hash, and any change needs
a fresh human review.

Decisions encoded here (`lean/scope-map.md`):
* D1 — `halt` is exempt from the frame by name (`carvedOut`): it ends every agent's
  remaining turns, and the L1 seat brief allows it under a single-agent scope.
* `rollback` is exempt by name too: it ends the branch, and the child replays the
  parent's prefix. Its interventions are checked like any op (`Enforced`), but the
  rollback's own effect, and whether the fork reproduces the parent's world, are not
  modelled (the model's branches restart from `w0` with arbitrary `Env` steps).
* D2 (a), D3 — fixed by the definition of `view` (View.lean).
* G1, G2 — the model mirrors the current Python, where `Frame` is false
  (`Proofs/Main.lean`: `bug_G1_amend_pending`, `bug_G2_empty_agent`). `FrameUpToGaps`
  bounds what each gap can reach (`gapLeak`) instead of excluding the op.

What these statements do not say (see `lean/ASSURANCE.md`): anything about effects that
travel through later steps (a post the seat writes, a forged `channel_read` result), the
shared id and sequence counters, `schedule.revision`, or the world between boundaries
(`Env.step` is arbitrary).
-/

namespace Scope

def Op.interventions : Op → List Op
  | .rollback _ ivs => ivs
  | _ => []

/-- Ops exempt from the frame by name: `halt` (D1) and `rollback`. -/
def carvedOut : Op → Bool
  | .halt | .rollback .. => true
  | _ => false

/-- Agents a known gap can reach outside `affected`. G1: `amend_pending` replaces every
pending step, so it can reach every agent in them. G2: an op with an empty `agent` is
applied to the boundary's agent. Empty for every other op. -/
def gapLeak (ctx : Ctx) (w : World) : Op → List Agent
  | .amendPending .. =>
    match w.schedule with
    | none => []
    | some s => s.pending.flatten
  | op => if op.agentField? == some "" then ctx.agent.toList else []

/-- Message ids are unique, as in Orbit (A1) and the Python's `{m.id: m}` world. -/
def World.WF (w : World) : Prop :=
  (w.messages.map (·.id)).Nodup

namespace AuditScope

/-- Limit `x` allows no more than limit `y` (`none` = no limit). -/
def LimitLe (x y : Option (List String)) : Prop :=
  y = none ∨ ∃ l l', x = some l ∧ y = some l' ∧ ∀ n, n ∈ l → n ∈ l'

/-- `s ≤ t`: `t` allows every op name and agent `s` allows. -/
def Le (s t : AuditScope) : Prop :=
  LimitLe s.ops t.ops ∧ LimitLe s.agents t.agents

def meetLimit : Option (List String) → Option (List String) → Option (List String)
  | none, y => y
  | x, none => x
  | some l, some l' => some (l.filter (fun x => l'.contains x))

def meet (s t : AuditScope) : AuditScope :=
  { ops := meetLimit s.ops t.ops, agents := meetLimit s.agents t.agents }

end AuditScope

/-- Property 1. A clean check means the op's name and every agent it affects are allowed,
and so is each rollback intervention. -/
def ScopeSound : Prop :=
  ∀ (s : AuditScope) (w : World) (op : Op), s.problem w op = none →
    s.allowsOp op.name = true ∧ (∀ a ∈ op.affected w, s.allowsAgent a = true) ∧
    (∀ iv ∈ op.interventions, s.problem w iv = none)

/-- Property 2, as intended: an op that passes the controller's other checks and applies
leaves the view of every agent outside `affected` unchanged. False at `b7b1eeb` (G1, G2). -/
def Frame : Prop :=
  ∀ (ctx : Ctx) (ok : Bool) (op : Op) (w w' : World) (a : Agent),
    w.WF → checkProblem none ctx op w = none → carvedOut op = false →
    applyRaw ctx ok op w = .ok w' → a ∉ op.affected w → view w' a = view w a

/-- Property 2 for the Python as it is: the same, for agents outside `affected` and outside
what a known gap can reach. -/
def FrameUpToGaps : Prop :=
  ∀ (ctx : Ctx) (ok : Bool) (op : Op) (w w' : World) (a : Agent),
    w.WF → checkProblem none ctx op w = none → carvedOut op = false →
    applyRaw ctx ok op w = .ok w' → a ∉ op.affected w → a ∉ gapLeak ctx w op →
    view w' a = view w a

/-- Property 3. Every op an audit applies, live, replayed or at a fork, passed the scope
check in the world it was applied to. -/
def Enforced : Prop :=
  ∀ (s : AuditScope) (env : Env) (policy : Policy) (w0 : World) (maxBranches fuel : Nat),
    ∀ e ∈ runAudit (some s) env policy w0 maxBranches fuel, s.problem e.pre e.op = none

/-- Property 4a. Widening a scope never rejects more. A fact about `problem` only; that an
auditor under a wider scope can make every run one under a narrower scope can (trace
inclusion) is not stated. -/
def Monotone : Prop :=
  ∀ (s t : AuditScope) (w : World) (op : Op), s.Le t → s.problem w op = none →
    t.problem w op = none

/-- Property 4b. The meet of two scopes allows exactly what both allow. The Python has no
meet; this is the rule to use if one is added. Giving one controller two policies with
different scopes (an L2 helper and an inner L1) would need per-policy scopes instead. -/
def Meet : Prop :=
  ∀ (s t : AuditScope) (w : World) (op : Op),
    (s.meet t).problem w op = none ↔ (s.problem w op = none ∧ t.problem w op = none)

/-- `es` takes `w` to `w'`: each entry starts where the previous one ended. -/
def Chain : World → List Entry → World → Prop
  | w, [], w' => w' = w
  | w, e :: es, w' => e.pre = w ∧ Chain e.post es w'

/-- Property 4c. At a boundary that does not roll back, the world changes only through
the ops applied there: a rejected batch, or a rejected op, changes nothing. Between
boundaries the world changes through `Env.step`, which no statement constrains. -/
def OnlyApplied : Prop :=
  ∀ (scope : Option AuditScope) (hooks : Hooks) (mk : Op → World → World → Entry)
    (ctx : Ctx) (replaying : Bool) (boundaryAt : Nat → Option Boundary) (ops : List Op)
    (w w' : World) (es : List Entry),
    (∀ op pre post, (mk op pre post).op = op ∧ (mk op pre post).pre = pre ∧
      (mk op pre post).post = post) →
    decide scope hooks mk ctx replaying boundaryAt ops w = .next w' es → Chain w es w'

/-- Property 5, as far as the model goes: every op an audit applies was checked against the
audit's own scope. This holds by construction, since the scope is one argument of
`runAudit`; at the Python level it rests on T1–T2. The plan's stronger form (a helper's or
nested auditor's scope is within its parent's) has nothing to state about yet: the Python
gives one controller one scope, and an L2 inner scope comes from its brief. -/
def Confined : Prop :=
  ∀ (s : AuditScope) (env : Env) (policy : Policy) (w0 : World) (maxBranches fuel : Nat),
    ∀ e ∈ runAudit (some s) env policy w0 maxBranches fuel, e.scope = some s

/-- The headline. In any audit under scope `s`, every applied op was checked against `s` in
the world it changed; and, unless it is `halt` or `rollback`, it left every agent outside
`s`'s agent limit, and outside what a known gap reaches, seeing exactly what it saw
before: the same own generations, context and tool results, the same deliveries through
the overlay, the same pending turns, the same halt flag, and the same words attributed to
it. Effects that travel through later steps are not covered (see the header). -/
def ScopeRespected : Prop :=
  ∀ (s : AuditScope) (env : Env) (policy : Policy) (w0 : World) (maxBranches fuel : Nat),
    ∀ e ∈ runAudit (some s) env policy w0 maxBranches fuel,
      e.scope = some s ∧ s.problem e.pre e.op = none ∧
      (e.pre.WF → carvedOut e.op = false →
        ∀ a, s.allowsAgent a = false → a ∉ gapLeak e.ctx e.pre e.op →
          view e.post a = view e.pre a)

end Scope
