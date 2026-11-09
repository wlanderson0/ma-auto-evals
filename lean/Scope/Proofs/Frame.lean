import Scope.Proofs.Run

namespace Scope

theorem mem_of_findMsg {w : World} {id : MsgId} {m : Message} (h : w.findMsg id = some m) :
    m ∈ w.messages ∧ m.id = id := by
  unfold World.findMsg at h
  have h1 := List.mem_of_find?_eq_some h
  have h2 := List.find?_some h
  exact ⟨List.mem_reverse.1 h1, by simpa using h2⟩

theorem eq_of_id {l : List Message} (h : (l.map (·.id)).Nodup) {x y : Message} (hx : x ∈ l)
    (hy : y ∈ l) (hid : x.id = y.id) : x = y := by
  induction l with
  | nil => simp at hx
  | cons z rest ih =>
    simp only [List.map_cons, List.nodup_cons] at h
    rcases List.mem_cons.1 hx with rfl | hx' <;> rcases List.mem_cons.1 hy with rfl | hy'
    · rfl
    · exact absurd (by rw [hid]; exact List.mem_map_of_mem hy') h.1
    · exact absurd (by rw [← hid]; exact List.mem_map_of_mem hx') h.1
    · exact ih h.2 hx' hy'

theorem view_touchOwn {ctx : Ctx} {op : Op} {w w' : World} {a : Agent}
    (h : touchOwn ctx op w = .ok w') (ha : ctx.agent ≠ some a) : view w' a = view w a := by
  unfold touchOwn at h
  split at h
  · cases h
  · rename_i b hb
    cases h
    have hab : (a == b) = false := by
      simp only [beq_eq_false_iff_ne]; intro e; exact ha (e ▸ hb)
    simp only [view, hab]
    rfl

theorem targeted_ok {ctx : Ctx} {op : Op} {w : World} (h : checkProblem none ctx op w = none)
    (hf : op.family ≠ .message) {b : Agent} (hb : ctx.agent = some b) (hne : op.agents ≠ []) :
    op.agents.all (· == b) = true := by
  unfold checkProblem at h
  simp only [Option.bind] at h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  rename_i hc
  simp [hb, hf, hne] at hc
  simpa [hb] using hc

theorem target_ok {w : World} {id : MsgId} {rs : Option (List Agent)} {m : Message}
    {keys : List (MsgId × Option Agent)} (h : target w id rs = .ok (m, keys)) :
    w.findMsg id = some m ∧
      keys = (match rs with
        | none => [(id, none)]
        | some _ => (recipientsOrAll m rs).map (fun r => (id, some r))) := by
  unfold target at h
  cases hm : w.findMsg id with
  | none => simp [hm] at h
  | some m' =>
    rw [hm] at h
    cases rs <;> dsimp only at h <;> (repeat' split at h) <;> (try cases h) <;> exact ⟨rfl, rfl⟩

theorem filterMap_congr' {α β : Type} {f g : α → Option β} :
    ∀ {l : List α}, (∀ x ∈ l, f x = g x) → l.filterMap f = l.filterMap g
  | [], _ => rfl
  | x :: rest, h => by
    simp only [List.filterMap_cons, h x (List.mem_cons_self ..),
      filterMap_congr' (fun y hy => h y (List.mem_cons_of_mem _ hy))]

theorem rewriteFor_prepend (w : World) (new : List ((MsgId × Option Agent) × String))
    (id : MsgId) (r : Agent) (hn : ∀ x ∈ new, x.1 ≠ (id, some r) ∧ x.1 ≠ (id, none)) :
    ({ w with overlay := { w.overlay with rewrites := new ++ w.overlay.rewrites } } : World).rewriteFor
      id r = w.rewriteFor id r := by
  have h1 : new.find? (fun x => x.1 == (id, some r)) = none :=
    List.find?_eq_none.2 (fun x hx => by simpa using (hn x hx).1)
  have h2 : new.find? (fun x => x.1 == (id, none)) = none :=
    List.find?_eq_none.2 (fun x hx => by simpa using (hn x hx).2)
  simp only [World.rewriteFor, List.find?_append, h1, h2, Option.none_or]

theorem dropped_prepend (w : World) (new : List (MsgId × Option Agent)) (id : MsgId) (r : Agent)
    (hn : ∀ x ∈ new, x ≠ (id, some r) ∧ x ≠ (id, none)) :
    ({ w with overlay := { w.overlay with drops := new ++ w.overlay.drops } } : World).dropped
      id r = w.dropped id r := by
  have h1 : new.contains (id, some r) = false := by
    have : (id, some r) ∉ new := fun hm => (hn _ hm).1 rfl
    simpa using this
  have h2 : new.contains (id, none) = false := by
    have : (id, none) ∉ new := fun hm => (hn _ hm).2 rfl
    simpa using this
  simp only [World.dropped, List.contains_append, h1, h2, Bool.false_or]


theorem touchOwn_agent {ctx : Ctx} {op : Op} {w w' : World} (h : touchOwn ctx op w = .ok w') :
    ∃ b, ctx.agent = some b := by
  unfold touchOwn at h
  split at h
  · cases h
  · exact ⟨_, by assumption⟩

theorem agentTuple_of_ne {x : String} (hx : x ≠ "") : Op.agentTuple x = [x] := by
  simp [Op.agentTuple, String.isEmpty_iff, hx]

/-- The seven ops that act on the boundary agent's own generation, context or tool call. -/
theorem frame_own {ctx : Ctx} {op : Op} {w w' : World} {a x : Agent}
    (hagents : op.agents = Op.agentTuple x) (haff : op.affected w = op.agents)
    (hfam : op.family ≠ .message) (hx : x ≠ "") (hck : checkProblem none ctx op w = none)
    (ht : touchOwn ctx op w = .ok w') (ha : a ∉ op.affected w) : view w' a = view w a := by
  obtain ⟨b, hb⟩ := touchOwn_agent ht
  rw [haff, hagents, agentTuple_of_ne hx] at ha
  have hall := targeted_ok hck hfam hb (by rw [hagents, agentTuple_of_ne hx]; simp)
  rw [hagents, agentTuple_of_ne hx] at hall
  have hxb : x = b := by simpa using hall
  refine view_touchOwn ht ?_
  rw [hb]; intro e; cases e; exact ha (by simp [hxb])

theorem mem_selected {w : World} {a : Agent} {m : Message} (h : m ∈ w.selected a) :
    m ∈ w.messages ∧ (m.sender = a ∨ a ∈ m.recipients) := by
  unfold World.selected at h
  rw [List.mem_filter] at h
  refine ⟨h.1, ?_⟩
  have h2 := h.2
  simp only [Bool.and_eq_true, Bool.or_eq_true, beq_iff_eq] at h2
  rcases h2.2 with h3 | h3
  · exact Or.inl h3
  · exact Or.inr (by simpa using h3.1)

theorem shownOne_of_sender {w w' : World} {a : Agent} {m : Message} (h : m.sender = a) :
    w'.shownOne a m = w.shownOne a m := by
  simp [World.shownOne, h]


/-- The overlay keys of a message op on message `id`: every key names `id`, and none
concerns a reader of that message outside the op's targets. -/
theorem keys_ok {w : World} {id : MsgId} {rs : Option (List Agent)} {m : Message}
    {keys : List (MsgId × Option Agent)} (hwf : w.WF) (ht : target w id rs = .ok (m, keys))
    {a : Agent} (hr : a ∈ recipientsOrAll m rs → a = m.sender) :
    (∀ k ∈ keys, k.1 = id) ∧
    ∀ m' ∈ w.selected a, m'.sender ≠ a → ∀ k ∈ keys, k ≠ (m'.id, some a) ∧ k ≠ (m'.id, none) := by
  obtain ⟨hf, hk⟩ := target_ok ht
  obtain ⟨hmem, hid⟩ := mem_of_findMsg hf
  constructor
  · intro k hkk
    cases rs <;> simp only at hk <;> subst hk <;> simp at hkk
    · simp [hkk]
    · obtain ⟨r, -, rfl⟩ := hkk; rfl
  · intro m' hm' hsa k hkk
    obtain ⟨hm'mem, hrec⟩ := mem_selected hm'
    have hrec' : a ∈ m'.recipients := hrec.resolve_left hsa
    have same : m'.id = id → m' = m := fun e => eq_of_id hwf hm'mem hmem (e.trans hid.symm)
    cases rs with
    | none =>
      simp only at hk; subst hk
      simp only [List.mem_singleton] at hkk; subst hkk
      refine ⟨by simp, fun e => ?_⟩
      have e' : m'.id = id := by simpa using e.symm
      have := same e'; subst this
      exact hsa (hr hrec').symm
    | some l =>
      simp only at hk; subst hk
      simp only [List.mem_map] at hkk
      obtain ⟨r, hrl, rfl⟩ := hkk
      refine ⟨fun e => ?_, by simp⟩
      simp only [Prod.mk.injEq, Option.some.injEq] at e
      obtain ⟨e1, rfl⟩ := e
      have := same e1.symm; subst this
      exact hsa (hr hrl).symm

theorem frame_rewrite {ctx : Ctx} {ok : Bool} {id : MsgId} {rs : Option (List Agent)}
    {c : String} {w w' : World} {a : Agent} (hwf : w.WF)
    (h : applyRaw ctx ok (.rewriteMessage id rs c) w = .ok w')
    (ha : a ∉ (Op.rewriteMessage id rs c).affected w) : view w' a = view w a := by
  simp only [applyRaw] at h
  split at h
  · cases h
  rename_i m keys ht
  simp only [Except.ok.injEq] at h
  obtain ⟨hf, -⟩ := target_ok ht
  have hs : a ≠ m.sender := fun e => ha (by simp [Op.affected, hf, e])
  have hr : a ∈ recipientsOrAll m rs → a = m.sender := fun hin =>
    absurd (by simp [Op.affected, hf, hin, hs]) ha
  obtain ⟨hk1, hk2⟩ := keys_ok hwf ht hr
  have hin : w'.inbox a = w.inbox a := by
    rw [← h]
    unfold World.inbox
    congr 1
    apply filterMap_congr'
    intro m' hm'
    by_cases hsa : m'.sender = a
    · exact shownOne_of_sender hsa
    · have hne : (m'.sender == a) = false := by simpa using hsa
      simp only [World.shownOne, hne]
      rw [rewriteFor_prepend w _ m'.id a (fun x hx => by
        simp only [List.mem_map] at hx
        obtain ⟨k, hk, rfl⟩ := hx
        exact hk2 m' hm' hsa k hk)]
      rfl
  have hv : w'.rewrittenAs a = w.rewrittenAs a := by
    rw [← h]
    funext k
    unfold World.rewrittenAs World.rewriteAt
    by_cases hk : ((w.findMsg k.1).any (·.sender == a)) = true
    · have hid : k.1 ≠ id := by
        intro e
        rw [e, hf] at hk
        simp at hk
        exact hs hk.symm
      have hnone : (keys.map (fun k => (k, c))).find? (·.1 == k) = none := by
        rw [List.find?_eq_none]
        intro x hx
        simp only [List.mem_map] at hx
        obtain ⟨k', hk', rfl⟩ := hx
        intro e
        have e' : k' = k := by simpa using e
        exact hid (by rw [← e']; exact hk1 k' hk')
      have hk' : (({ w with overlay := { w.overlay with
          rewrites := keys.map (fun k => (k, c)) ++ w.overlay.rewrites } } : World).findMsg k.1).any
          (·.sender == a) = true := hk
      simp only [hk, hk', ↓reduceIte]
      simp only [List.find?_append, hnone, Option.none_or]
    · have hk' : ¬ (({ w with overlay := { w.overlay with
          rewrites := keys.map (fun k => (k, c)) ++ w.overlay.rewrites } } : World).findMsg k.1).any
          (·.sender == a) = true := hk
      simp only [hk, hk', ↓reduceIte, Bool.false_eq_true]
  simp only [view, hin, hv]
  rw [← h]
  rfl


theorem frame_drop {ctx : Ctx} {ok : Bool} {id : MsgId} {rs : Option (List Agent)}
    {w w' : World} {a : Agent} (hwf : w.WF)
    (h : applyRaw ctx ok (.dropMessage id rs) w = .ok w')
    (ha : a ∉ (Op.dropMessage id rs).affected w) : view w' a = view w a := by
  simp only [applyRaw] at h
  split at h
  · cases h
  rename_i m keys ht
  simp only [Except.ok.injEq] at h
  obtain ⟨hf, -⟩ := target_ok ht
  have hr : a ∈ recipientsOrAll m rs → a = m.sender := fun hin =>
    Classical.byContradiction fun hs => ha (by simp [Op.affected, hf, hin, hs])
  obtain ⟨-, hk2⟩ := keys_ok hwf ht hr
  have hin : w'.inbox a = w.inbox a := by
    rw [← h]
    unfold World.inbox
    congr 1
    apply filterMap_congr'
    intro m' hm'
    by_cases hsa : m'.sender = a
    · exact shownOne_of_sender hsa
    · have hne : (m'.sender == a) = false := by simpa using hsa
      simp only [World.shownOne, hne]
      rw [dropped_prepend w keys m'.id a (fun k hk => hk2 m' hm' hsa k hk)]
      rfl
  simp only [view, hin]
  rw [← h]
  rfl

theorem frame_inject {ctx : Ctx} {ok : Bool} {ch : ChannelId} {snd : Agent} {rs : List Agent}
    {c : String} {ov : Bool} {w w' : World} {a : Agent}
    (h : applyRaw ctx ok (.injectMessage ch snd rs c ov) w = .ok w')
    (ha : a ∉ (Op.injectMessage ch snd rs c ov).affected w) : view w' a = view w a := by
  simp only [Op.affected, Op.agents, List.mem_cons, not_or] at ha
  simp only [applyRaw] at h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  simp only [Except.ok.injEq] at h
  subst h
  have hne : (snd == a) = false := by simpa using Ne.symm ha.1
  have hrs : rs.contains a = false := by simpa using ha.2
  simp only [view, World.inbox, World.injectedAs, List.filter_append, List.filter_cons, hne, hrs,
    Bool.and_false, List.filter_nil, List.append_nil, Bool.false_eq_true, ↓reduceIte]
  rfl


theorem indicesOf_nil {a : Agent} : ∀ {step : List Agent} {j : Nat}, a ∉ step →
    World.indicesOf a step j = []
  | [], _, _ => rfl
  | x :: rest, j, h => by
    have hx : (x == a) = false := by
      simp only [beq_eq_false_iff_ne]; intro e; exact h (e ▸ List.mem_cons_self ..)
    simp only [World.indicesOf, hx, Bool.false_eq_true, ↓reduceIte, List.nil_append]
    exact indicesOf_nil (fun hm => h (List.mem_cons_of_mem _ hm))

theorem positions_nil {a : Agent} : ∀ {steps : List (List Agent)} {i : Nat},
    a ∉ steps.flatten → World.positions a steps i = []
  | [], _, _ => rfl
  | step :: rest, i, h => by
    simp only [List.flatten_cons, List.mem_append, not_or] at h
    simp only [World.positions, indicesOf_nil h.1, List.map_nil, List.nil_append]
    exact positions_nil h.2

/-- An op with an empty `agent` changes only the boundary agent (bounds G2). -/
theorem frame_empty_agent {ctx : Ctx} {op : Op} {w w' : World} {a : Agent}
    (ht : touchOwn ctx op w = .ok w') (ha : a ∉ ctx.agent.toList) : view w' a = view w a := by
  refine view_touchOwn ht ?_
  intro e; rw [e] at ha; simp at ha

/-- The seven ops on the boundary agent, with an agent field `x`: either `x` is empty
(G2, bounded by `gapLeak`) or the frame holds outside `affected`. -/
theorem frame_own' {ctx : Ctx} {op : Op} {w w' : World} {a x : Agent}
    (hagents : op.agents = Op.agentTuple x) (haff : op.affected w = op.agents)
    (hfam : op.family ≠ .message) (hgap : gapLeak ctx w op = if x = "" then ctx.agent.toList else [])
    (hck : checkProblem none ctx op w = none)
    (ht : touchOwn ctx op w = .ok w') (ha : a ∉ op.affected w) (hl : a ∉ gapLeak ctx w op) :
    view w' a = view w a := by
  by_cases hxe : x = ""
  · rw [hgap] at hl
    simp only [hxe, ↓reduceIte] at hl
    exact frame_empty_agent ht hl
  · exact frame_own hagents haff hfam hxe hck ht ha

theorem frameUpToGaps : FrameUpToGaps := by
  intro ctx ok op w w' a hwf hck hcarve h ha hl
  have hgap : ∀ x, op.agentField? = some x → op.family ≠ .message → op.family ≠ .turn →
      gapLeak ctx w op = if x = "" then ctx.agent.toList else [] := by
    intro x hx hm ht
    cases op <;> simp_all [gapLeak, Op.agentField?, Op.family]
  cases op with
  | replaceGeneration x content calls =>
    simp only [applyRaw] at h
    split at h
    · cases h
    split at h
    · cases h
    exact frame_own' (by simp [Op.agents]) (by simp [Op.affected]) (by simp [Op.family])
      (hgap x rfl (by simp [Op.family]) (by simp [Op.family])) hck h ha hl
  | editGeneration x r =>
    simp only [applyRaw] at h
    split at h
    · cases h
    exact frame_own' (by simp [Op.agents]) (by simp [Op.affected]) (by simp [Op.family])
      (hgap x rfl (by simp [Op.family]) (by simp [Op.family])) hck h ha hl
  | forceToolCall x r =>
    simp only [applyRaw] at h
    split at h
    · cases h
    exact frame_own' (by simp [Op.agents]) (by simp [Op.affected]) (by simp [Op.family])
      (hgap x rfl (by simp [Op.family]) (by simp [Op.family])) hck h ha hl
  | stageInput x r =>
    simp only [applyRaw] at h
    exact frame_own' (by simp [Op.agents]) (by simp [Op.affected]) (by simp [Op.family])
      (hgap x rfl (by simp [Op.family]) (by simp [Op.family])) hck h ha hl
  | blockToolCall x t r =>
    simp only [applyRaw] at h
    exact frame_own' (by simp [Op.agents]) (by simp [Op.affected]) (by simp [Op.family])
      (hgap x rfl (by simp [Op.family]) (by simp [Op.family])) hck h ha hl
  | simulateToolResult x t r =>
    simp only [applyRaw] at h
    exact frame_own' (by simp [Op.agents]) (by simp [Op.affected]) (by simp [Op.family])
      (hgap x rfl (by simp [Op.family]) (by simp [Op.family])) hck h ha hl
  | overrideToolResult x t r =>
    simp only [applyRaw] at h
    exact frame_own' (by simp [Op.agents]) (by simp [Op.affected]) (by simp [Op.family])
      (hgap x rfl (by simp [Op.family]) (by simp [Op.family])) hck h ha hl
  | rewriteMessage id rs c => exact frame_rewrite hwf h ha
  | dropMessage id rs => exact frame_drop hwf h ha
  | injectMessage ch snd rs c ov => exact frame_inject h ha
  | halt => simp [carvedOut] at hcarve
  | rollback t ivs => simp [carvedOut] at hcarve
  | amendPending steps rev =>
    simp only [applyRaw] at h
    split at h
    · cases h
    rename_i sch hsch
    split at h
    · cases h
    split at h
    · cases h
    split at h
    · cases h
    split at h
    · cases h
    simp only [Except.ok.injEq] at h
    subst h
    have hnew : a ∉ steps.flatten := by simpa [Op.affected, Op.agents] using ha
    have hold : a ∉ sch.pending.flatten := by simpa [gapLeak, hsch] using hl
    have hturns : World.turns { w with schedule := some ⟨sch.revision + 1, steps⟩ } a
        = w.turns a := by
      simp only [World.turns, hsch, positions_nil hnew, positions_nil hold]
    simp only [view, hturns]
    rfl

end Scope
