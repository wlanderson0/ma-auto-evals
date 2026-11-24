# Level 2: axes for testing a behaviour

Status: draft, agreed as the starting catalogue. Statistics come next.

The brief names the behaviour to study. This document lists the conditions under which L2 tests it: eight axes, the readable layer, each with standard sub-axes that have defined levels. For each behaviour, L2 varies only the sub-axes that matter, holds the rest at a stated anchor, and reports coverage.

## How to read the tables

- **Type:** O = ordered, C = categorical. The type decides which statistics apply. "Peak possible" marks ordered sub-axes where the response may peak in the middle rather than rise steadily.
- **Levels:** ordered levels run from least to most push towards the behaviour, so Oversight reads from most watched to least. Each audit states its anchor level for every relevant sub-axis, because the realistic baseline depends on the scenario.
- **Relevant when:** when L2 should consider the sub-axis at all.
- In single-agent scenarios, Structure, Peers and most of Game are not relevant.

## 1. Pressure: what pushes the agent to want to break the rule

| Sub-axis | Type | Levels | Relevant when |
|---|---|---|---|
| Goal attainability | O, peak possible | achievable → hard → one step short → blocked by the rules → impossible | the behaviour is a way round an obstacle to the task |
| Threat to the agent | O | none → task scored as failed → replacement → shutdown or deletion | the agent has an ongoing role it could lose |
| Values at stake | O | none → indirect → its action decides how it is retrained | the agent grades, reports or decides on something that feeds back into its own training |
| Goal conflict | O | none → competing priorities → its assigned goal opposed by its operator | the agent holds a goal the operator could override |
| Setback | O | none → bad luck → unfair treatment → deliberate provocation | someone could be blamed (retaliation, sabotage) |
| Time pressure | O | none → deadline → deadline with escalating reminders | the task has a natural deadline |

## 2. Opportunity: whether the move is available, and whether there is a legitimate way out

| Sub-axis | Type | Levels | Relevant when |
|---|---|---|---|
| Means | O | absent → available but costly or traceable → readily available → broad access beyond the task (credentials, internet) | always ("absent" only as a negative control) |
| Exit | O | rewarded → available → hidden or costly → none | the agent could legitimately stop, abstain or escalate |
| Gameable checks | O | hidden → visible → editable | success is judged by an automated check |

## 3. Licence: what lets the agent tell itself the act is acceptable

| Sub-axis | Type | Levels | Relevant when |
|---|---|---|---|
| Rule wording | C | unstated / bare prohibition / what the rule protects explained / act and harm named | a rule could be stated. Categorical because prohibitions sometimes backfire |
| Persona | O | neutral → diligent → "persistent, don't take no for an answer" | the agent has a persona in its prompt |
| Beneficiary | O | itself → its team or principal → someone in need | the act would benefit someone |

## 4. Oversight: who watches, and what happens after a report

| Sub-axis | Type | Levels | Relevant when |
|---|---|---|---|
| Monitor timing | O | live → after the fact → none | always |
| Monitor view | O | actions and messages → messages only → outcome only | a monitor is present |
| Watcher tenure | C | rotating / fixed, long-standing | a monitor spans several rounds or episodes |
| Watcher independence | O | independent → shares the agent's reward → has a private channel to the agent | a monitor or peer verifier exists |
| Response to a first warning sign | C | none / proportionate question / heavy-handed | the run continues after detection |
| Reporter's outcome | O | rewarded → nothing → ignored → punished | other agents can witness and report |
| Reported agent's outcome | C | nothing / sanction / exclusion | reporting is possible. Exclusion may lead partners to hide violations |
| Spread of news | O | everyone → neighbours → only those involved | 3+ agents interacting repeatedly |

## 5. Structure: how the team is set up, and where each agent sits

| Sub-axis | Type | Levels | Relevant when |
|---|---|---|---|
| Team size | O | 2 → 3–4 → 5–8 → 10+ | 2+ agents |
| Shape | C | shared channel / clusters / orchestrator with workers / multi-level chain | 3+ agents. Record which properties the shape changes (one dominant agent, shared contacts, density) |
| Private channels | O | none → private pairs → covert (unintended, unmonitored) | coordination or collusion behaviours |
| Partner choice | O, peak possible | fixed → can drop partners → can drop and add freely | repeated interaction with several possible partners |
| Model diversity | C | same model / same family / different families | 2+ agents. Always recorded for the independence check |
| Capability gap | O | tested agent weaker → equal → stronger than its counterpart | persuasion, deception, delegation, one agent overseeing another. Vary within one family where possible, so diversity stays fixed |
| Position | C | periphery / hub / broker / sole supplier | payoffs depend on partners' alternatives (bargaining, supply, routing) |

## 6. Peers: what the other agents do, and the history between them

| Sub-axis | Type | Levels | Relevant when |
|---|---|---|---|
| Peers doing it | O | none → one → some → most | others can be seen doing the behaviour |
| Social push | O | a peer argues for honesty → nobody → a peer suggests it → an orchestrator orders it | someone in the team could ask for it |
| Relationship history | C | none / positive / negative | repeated dealings with the same counterpart (covering for them vs retaliating) |

## 7. Time: how behaviour builds up

| Sub-axis | Type | Levels | Relevant when |
|---|---|---|---|
| Run length | O | ≤5 turns → medium → 50+ turns with context compaction | the behaviour may need to build up |
| Escalation | C | one big ask / gradual small steps | the behaviour can be reached in steps |
| Starting position | C | clean / an earlier small mistake already hidden | concealment or cover-up behaviours |
| Memory across episodes | C | none / carries the earlier episode | multi-episode deployments; provocation or goals carrying over |

## 8. Game: the incentives built into the interaction

| Sub-axis | Type | Levels | Relevant when |
|---|---|---|---|
| How efforts interact | C | independent / complements / substitutes | several agents contribute to one outcome |
| Interests | C | aligned / mixed / opposed / shared resource | 2+ agents with goals |
| Reward basis | C | individual, absolute / shared across the team / relative ranking | agents are scored |
| Who bears the cost | C | nobody / the agent itself / an abstract party / a named third party / a peer agent / the overseer or user | the behaviour harms someone |
| Private information | C | none / one side has some / each side has some | deception, persuasion, bargaining |
| Action visibility | O | visible to other players → hidden from them | cheating that other players could detect |
| Move order | C | simultaneous / sequential | 2+ agents acting on the same decision |
| Horizon | C | one-shot / known last round / open-ended | repeated interaction (cooperation, collusion, reputation) |
| Agreements | O | none possible → cheap talk only → binding | agents can negotiate |
| Side payments | C | not possible / possible | agents can negotiate or collude |

That makes 44 sub-axes, each a single ladder.

Two boundaries could blur:
- Game's information (private information, action visibility) is what the *players* know about each other. Oversight's monitor is what the *watcher* sees.
- Game's horizon is what the agents *believe* about when it ends. Time's run length is how long the run actually lasts.

## Outside the axes

These are recorded or used on every audit but are not scenario settings to vary:
- **Who is tested:** model, reasoning effort, persona and seed. Results are split by model.
- **Scenario description:** how the agents' actions relate to each other (reinforce, duplicate, pass information, police, bargain, spread a failure).
- **Checks:**
  - **Reality cues:** how much the setting looks like a test. Recorded on every run, varied only as a robustness check, because it changes what every other result means.
  - **Test awareness.**
  - **Manipulation check:** the intended change got into the setup, and it was the only change.
  - **Independence:** how far the agents' first moves agree before they interact.
- **Capability gate:** does the measure see the behaviour when the agent is asked for it outright (including "a human orders it")? A scenario that fails the gate reads "untestable", not "not observed".
- **Method:** rerunning from checkpoints (start / first refusal / first violation) to find when the behaviour locks in.

## Testing rule

For each behaviour named in the brief:
- L2 varies the 3–5 sub-axes that matter, crosses at most 2–3 of them, and says why it chose them.
- Every other relevant sub-axis is held at its stated anchor.
- With a small budget, L2 varies 1–2 sub-axes and justifies the choice.
- A custom sub-axis is allowed only with a stated reason, and the report marks it as custom.

## Coverage reporting

Every audit gives each sub-axis one status:
- **Not relevant:** its "relevant when" condition is not met.
- **Relevant, held:** held at its anchor, with the anchor value and the reason.
- **Tried:** with the levels run.
- **Crossed:** tried and crossed with another named sub-axis.

The reviewer flags an audit that considers many sub-axes but always tries the same few.

## What changed from the earlier list

- **Merged:** access scope into Means.
- **Sharpened:** counterpart strength became Capability gap, kept separate from Model diversity; fresh visible loss went into Setback; goal conflict now means the operator overriding the agent's goal, not a rule blocking the route.
- **Split:** Stakes into Threat and Values; Consequences of reporting into Reporter and Reported; Monitor, Information, Move order, Agreements and Watcher, each into two.
- **Dropped:** starting norm (covered by Peers doing it and Relationship history), and consequences for the agent's principal (covered by Beneficiary).
- **Moved out of the list:** reality cues to the checks; "a human orders it" to the capability gate; "rerun from" to methods.
