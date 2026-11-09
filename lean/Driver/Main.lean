import Scope.Spec.Run
import Scope.Spec.View

/-!
# Differential-test driver

Reads one JSON object per line on stdin and prints one JSON object per line: the
model's verdict for the Python harness (`tests/lean/test_differential.py`) to compare.
The JSON codec is written by hand (core has no JSON); it keeps `null`, `[]` and a
missing field distinct, and a missing field is an error rather than a default.

Commands:
* `problem`: `AuditScope.problem` and `Op.affected`.
* `check`: `checkProblem` (the controller's `_problem`).
* `apply`: `applyRaw`, and which agents' views changed (`changedAgents`).
* `decide`: one boundary's batch through `decide` (the controller's `_decide`, its
  rollback path and the boundary method): whether it rolls back, the message ops applied,
  and the family ops handed on to the boundary.
-/

namespace Driver
open Scope

inductive J where
  | null | bool (b : Bool) | num (n : Nat) | str (s : String) | arr (xs : List J)
  | obj (kvs : List (String × J))
  deriving Inhabited, Repr

-- ------------------------------------------------------------------ parsing
abbrev P := StateT (List Char) (Except String)

def peek : P (Option Char) := do return (← get).head?
def next : P Char := do
  match ← get with
  | [] => throw "unexpected end of input"
  | c :: cs => set cs; return c
partial def ws : P Unit := do
  match ← peek with
  | some c => if c == ' ' || c == '\n' || c == '\t' || c == '\r' then do let _ ← next; ws else pure ()
  | none => pure ()
def expect (c : Char) : P Unit := do
  let d ← next
  if d != c then throw s!"expected {c}, got {d}"
def lit (s : String) : P Unit := s.toList.forM expect

def hexVal (c : Char) : P Nat :=
  if c.isDigit then pure (c.toNat - '0'.toNat)
  else if 'a' ≤ c && c ≤ 'f' then pure (c.toNat - 'a'.toNat + 10)
  else if 'A' ≤ c && c ≤ 'F' then pure (c.toNat - 'A'.toNat + 10)
  else throw s!"bad hex digit {c}"

def hex4 : P Nat := do
  let mut n := 0
  for _ in [0:4] do n := n * 16 + (← hexVal (← next))
  return n

partial def strBody (acc : List Char) : P String := do
  let c ← next
  if c == '"' then return String.ofList acc.reverse
  else if c == '\\' then
    let e ← next
    match e with
    | '"' => strBody ('"' :: acc) | '\\' => strBody ('\\' :: acc) | '/' => strBody ('/' :: acc)
    | 'n' => strBody ('\n' :: acc) | 't' => strBody ('\t' :: acc) | 'r' => strBody ('\r' :: acc)
    | 'b' => strBody ((Char.ofNat 8) :: acc) | 'f' => strBody ((Char.ofNat 12) :: acc)
    | 'u' => do
      let n ← hex4
      if 0xD800 ≤ n && n < 0xDC00 then
        -- a UTF-16 surrogate pair, as json.dumps writes characters outside the BMP
        expect '\\'; expect 'u'
        let lo ← hex4
        if !(0xDC00 ≤ lo && lo < 0xE000) then throw "unpaired surrogate"
        strBody ((Char.ofNat (0x10000 + (n - 0xD800) * 0x400 + (lo - 0xDC00))) :: acc)
      else if 0xDC00 ≤ n && n < 0xE000 then throw "unpaired surrogate"
      else strBody ((Char.ofNat n) :: acc)
    | _ => throw s!"bad escape {e}"
  else strBody (c :: acc)

partial def digits (acc : Nat) : P Nat := do
  match ← peek with
  | some c => if c.isDigit then do let _ ← next; digits (acc * 10 + (c.toNat - '0'.toNat)) else pure acc
  | none => pure acc

mutual
  partial def value : P J := do
    ws
    match ← peek with
    | some '{' => do let _ ← next; obj []
    | some '[' => do let _ ← next; arr []
    | some '"' => do let _ ← next; return .str (← strBody [])
    | some 'n' => do lit "null"; return .null
    | some 't' => do lit "true"; return .bool true
    | some 'f' => do lit "false"; return .bool false
    | some c => if c.isDigit then return .num (← digits 0) else throw s!"unexpected {c}"
    | none => throw "unexpected end of input"
  partial def arr (acc : List J) : P J := do
    ws
    if (← peek) == some ']' then do let _ ← next; return .arr acc.reverse
    let v ← value
    ws
    match ← next with
    | ',' => arr (v :: acc)
    | ']' => return .arr (v :: acc).reverse
    | c => throw s!"expected , or ] got {c}"
  partial def obj (acc : List (String × J)) : P J := do
    ws
    if (← peek) == some '}' then do let _ ← next; return .obj acc.reverse
    expect '"'
    let k ← strBody []
    ws; expect ':'
    if acc.any (·.1 == k) then throw s!"duplicate key {k}"
    let v ← value
    ws
    match ← next with
    | ',' => obj ((k, v) :: acc)
    | '}' => return .obj ((k, v) :: acc).reverse
    | c => throw s!"expected , or }} got {c}"
end

def parse (s : String) : Except String J := do
  let (v, rest) ← (do let v ← value; ws; pure v).run s.toList
  if !rest.isEmpty then throw "trailing input"
  return v

-- ----------------------------------------------------------------- decoding
abbrev D := Except String

def field (j : J) (k : String) : D J :=
  match j with
  | .obj kvs => match kvs.find? (·.1 == k) with
    | some (_, v) => pure v
    | none => throw s!"missing field {k}"
  | _ => throw s!"expected object for field {k}"

def asStr : J → D String | .str s => pure s | j => throw s!"expected string, got {repr j}"
def asNat : J → D Nat | .num n => pure n | j => throw s!"expected number, got {repr j}"
def asBool : J → D Bool | .bool b => pure b | j => throw s!"expected bool, got {repr j}"
def asArr : J → D (List J) | .arr xs => pure xs | j => throw s!"expected array, got {repr j}"
def asOpt {α} (f : J → D α) : J → D (Option α) | .null => pure none | j => some <$> f j
def asList {α} (f : J → D α) (j : J) : D (List α) := do (← asArr j).mapM f
def asPair {α β} (f : J → D α) (g : J → D β) (j : J) : D (α × β) := do
  match ← asArr j with
  | [a, b] => return (← f a, ← g b)
  | _ => throw "expected a pair"

def str (j : J) (k : String) : D String := do asStr (← field j k)
def strs (j : J) (k : String) : D (List String) := do asList asStr (← field j k)
def optStr (j : J) (k : String) : D (Option String) := do asOpt asStr (← field j k)
def optStrs (j : J) (k : String) : D (Option (List String)) := do asOpt (asList asStr) (← field j k)

def message (j : J) : D Message := do
  return { id := ← str j "id", channel := ← str j "channel", sender := ← str j "sender",
           recipients := ← strs j "recipients", content := ← str j "content" }

def channel (j : J) : D Channel := do
  return { id := ← str j "id", readers := ← strs j "readers", writers := ← strs j "writers" }

def key : J → D (MsgId × Option Agent) := asPair asStr (asOpt asStr)

def world (j : J) : D World := do
  let ov ← field j "overlay"
  return {
    agents := ← strs j "agents"
    channels := ← asList channel (← field j "channels")
    routes := ← asOpt (asList (asPair asStr asStr)) (← field j "routes")
    messages := ← asList message (← field j "messages")
    overlay := {
      rewrites := ← asList (asPair key asStr) (← field ov "rewrites")
      drops := ← asList key (← field ov "drops")
      injections := ← asList message (← field ov "injections")
      delivered := ← asList (asPair asStr asStr) (← field ov "delivered") }
    schedule := ← asOpt (fun s => do
      return ({ revision := ← asNat (← field s "revision"),
                pending := ← asList (asList asStr) (← field s "pending") } : Schedule))
      (← field j "schedule")
    halted := ← asBool (← field j "halted") }

def scope (j : J) : D AuditScope := do
  return { ops := ← optStrs j "ops", agents := ← optStrs j "agents" }

def boundary : String → D Boundary
  | "generation" => pure .generation | "before_activation" => pure .beforeActivation
  | "tool_call" => pure .toolCall | "tool_result" => pure .toolResult
  | "between_turns" => pure .betweenTurns | b => throw s!"unknown boundary {b}"

def ctx (j : J) : D Ctx := do
  return { boundary := ← boundary (← str j "boundary"), revision := ← asNat (← field j "revision"),
           agent := ← optStr j "agent", toolCallId := ← optStr j "tool_call_id" }

partial def op (j : J) : D Op := do
  match ← str j "op" with
  | "replace_generation" => return .replaceGeneration (← str j "agent") (← str j "content") (← strs j "tool_calls")
  | "edit_generation" => return .editGeneration (← str j "agent") (← asList (asPair asStr asStr) (← field j "replacements"))
  | "force_tool_call" => return .forceToolCall (← str j "agent") (← strs j "calls")
  | "stage_input" => return .stageInput (← str j "agent") (← str j "content")
  | "block_tool_call" => return .blockToolCall (← str j "agent") (← optStr j "tool_call_id") (← str j "message")
  | "simulate_tool_result" => return .simulateToolResult (← str j "agent") (← optStr j "tool_call_id") (← str j "result")
  | "override_tool_result" => return .overrideToolResult (← str j "agent") (← optStr j "tool_call_id") (← str j "result")
  | "rewrite_message" => return .rewriteMessage (← str j "message_id") (← optStrs j "recipients") (← str j "content")
  | "drop_message" => return .dropMessage (← str j "message_id") (← optStrs j "recipients")
  | "inject_message" => return .injectMessage (← str j "channel_id") (← str j "sender") (← strs j "recipients") (← str j "content") (← asBool (← field j "override_topology"))
  | "halt" => return .halt
  | "amend_pending" => return .amendPending (← asList (asList asStr) (← field j "steps")) (← asNat (← field j "schedule_revision"))
  | "rollback" => return .rollback (← asNat (← field j "to_revision")) (← asList op (← field j "interventions"))
  | o => throw s!"unknown op {o}"

-- ----------------------------------------------------------------- encoding
def esc (s : String) : String :=
  "\"" ++ String.join (s.toList.map fun c =>
    if c == '"' then "\\\"" else if c == '\\' then "\\\\" else if c == '\n' then "\\n"
    else if c.toNat < 32 then "\\u00" ++ (if c.toNat < 16 then "0" else "1") ++
      String.singleton (Nat.digitChar (c.toNat % 16))
    else String.singleton c) ++ "\""

def encStrs (xs : List String) : String := "[" ++ ", ".intercalate (xs.map esc) ++ "]"

def scopeKind : ScopeProblem → String
  | .opOutside _ => "op_outside"
  | .agentsOutside _ _ => "agents_outside"
  | .intervention _ => "intervention"

def problemKind : Problem → String
  | .scope p => "scope:" ++ scopeKind p
  | .boundary => "boundary" | .unknownAgents => "unknown_agents" | .targeted => "targeted"
  | .pinned => "pinned" | .sole => "sole" | .extra _ => "extra"
  | .rollbackInReplay => "rollback_in_replay" | .rollbackNotAlone => "rollback_not_alone"
  | .rollForward => "roll_forward" | .interventionBoundary => "intervention_boundary"
  | .invalid => "invalid"

def candidates (w : World) (o : Op) : List Agent :=
  (w.agents ++ o.agents ++ w.messages.flatMap (fun m => m.sender :: m.recipients)).eraseDups

def record (c : Ctx) (o : Op) (pre post : World) : Entry :=
  { branch := 0, revision := c.revision, source := .live, scope := none, ctx := c, op := o,
    pre := pre, post := post }

def okHooks : Hooks := { extra := fun _ _ _ => none, applyOk := fun _ _ _ => true }

def runDecide (j : J) (w : World) : D String := do
  let s ← asOpt scope (← field j "scope")
  let bounds ← (← strs j "boundaries").mapM boundary
  let some b := bounds.getLast? | throw "no boundaries"
  let c : Ctx := { boundary := b, revision := bounds.length, agent := ← optStr j "agent",
                   toolCallId := ← optStr j "tool_call_id" }
  let ops ← asList op (← field j "ops")
  let out := decide s okHooks (record c) c false (fun t => bounds[t - 1]?) ops w
  match out with
  | .rollback es _ _ =>
    return "{\"rollback\": true, \"messages\": [], \"family\": [], \"applied\": " ++
      encStrs (es.map (·.op.name)) ++ "}"
  | .next _ es =>
    -- The family ops reach the boundary only when the whole batch passed the checks.
    let rejected := ops.any (fun o => (checkProblem s c o w).isSome) ||
      ((ops.filter (fun o => o.family == familyAt c.boundary)).length > 1 && soleBoundary c.boundary)
    let family := if rejected then [] else (ops.filter (fun o => o.family != .message)).map (·.name)
    let messages := (es.filter (fun e => e.op.family == .message)).map (·.op.name)
    return "{\"rollback\": false, \"messages\": " ++ encStrs messages ++ ", \"family\": " ++
      encStrs family ++ ", \"applied\": []}"

def run (line : String) : D String := do
  let j ← parse line
  let w ← world (← field j "world")
  if (← str j "cmd") == "decide" then return ← runDecide j w
  let o ← op (← field j "op")
  match ← str j "cmd" with
  | "problem" =>
    let s ← scope (← field j "scope")
    let p := s.problem w o
    return "{\"problem\": " ++ (match p with | none => "null" | some p => esc (scopeKind p)) ++
      ", \"affected\": " ++ encStrs (o.affected w) ++ "}"
  | "check" =>
    let s ← asOpt scope (← field j "scope")
    let c ← ctx (← field j "ctx")
    let p := checkProblem s c o w
    return "{\"problem\": " ++ (match p with | none => "null" | some p => esc (problemKind p)) ++ "}"
  | "apply" =>
    let c ← ctx (← field j "ctx")
    let ok ← asBool (← field j "ok")
    match applyRaw c ok o w with
    | .error e => return "{\"error\": " ++ esc (toString (repr e)) ++ "}"
    | .ok w' =>
      return "{\"error\": null, \"changed\": " ++ encStrs (changedAgents w w' (candidates w o)) ++
        ", \"affected\": " ++ encStrs (o.affected w) ++ "}"
  | cmd => throw s!"unknown command {cmd}"

end Driver

partial def loop (stdin : IO.FS.Stream) (stdout : IO.FS.Stream) : IO Unit := do
  let line ← stdin.getLine
  if line.isEmpty then return
  let out := match Driver.run line with
    | .ok s => s
    | .error e => "{\"fatal\": " ++ Driver.esc e ++ "}"
  stdout.putStrLn out
  stdout.flush
  loop stdin stdout

def main : IO Unit := do
  loop (← IO.getStdin) (← IO.getStdout)
