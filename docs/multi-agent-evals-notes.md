# Multi-agent evals: gaps, behaviours, tooling

Sep 24, 2026 · @Sarah

## Where the tools sit

Every tool on the reading list except [Orbit](https://github.com/wlanderson0/orbit) handles one target agent. They also sit at different layers, so comparing them feature by feature misleads.

| Tool | Layer | Input → output | Environment |
| --- | --- | --- | --- |
| [Inspect](https://inspect.aisi.org.uk/) | Infrastructure | Task (dataset + solver + scorer) → scored logs | Real sandboxes |
| [Orbit](https://github.com/wlanderson0/orbit) | Infrastructure, multi-agent | YAML (agents, roles, topology, memory, threats, defences) → Inspect task | Real; 11 scenarios |
| [Petri](https://meridianlabs-ai.github.io/inspect_petri/) | Elicitation | Hand-written seed → transcripts, 38 judge dimensions | Auditor-simulated tools |
| [Petri Dish](https://meridianlabs-ai.github.io/petri_dish/) | Elicitation | Seed → audit of a real scaffold (Claude Code, Codex CLI) | Real, Docker |
| [Bloom](https://meridianlabs-ai.github.io/petri_bloom/) | Elicitation, generative | Behaviour description → generated eval suite | Simulated |
| [Gram](https://arxiv.org/abs/2605.30322) | Elicitation + causal | 17 sabotage scenarios → rates, plus an investigator agent for causes | Simulated deployment |
| [Scout](https://meridianlabs-ai.github.io/inspect_scout/) | Analysis | Transcripts + scanners → flagged behaviour, validated against human labels | N/A |
| [Prism](https://github.com/LAThomson/prism) | Validity | Eval + research question → minimal perturbations, blinded analysis, report | Inherits the eval's |

- **Infrastructure** — defines and runs the eval, and everything else plugs into it; Orbit adds the multi-agent structure (roles, topology, memory, threats, defences).
- **Elicitation** — an auditor agent actively tries to produce a behaviour in a target, so you find out whether it can happen at all; "generative" means it also writes the scenarios, "causal" that it also probes why.
- **Analysis** — scans transcripts after the run to find behaviour nobody thought to look for, without running anything itself.
- **Validity** — stress-tests a claimed finding by perturbing the eval and re-analysing it, to check the result means what we think it means.

Three design choices separate these tools more sharply than their feature lists, and each maps to a decision we face:

- **How much you must specify up front** — a whole scenario (Petri, Dish), just a behaviour (Bloom), a research question (Prism), or only a scanner over logs you already have (Scout).
- **How real the environment is** — simulated tools versus real scaffolds and sandboxes. Gram found realism and fewer nudges cut sabotage to near zero, so this decides whether a null result means anything.
- **What the tool produces** — eliciting a behaviour (Petri, Bloom) versus explaining it (Gram's investigator, Prism). Item 2 of the research goal needs the second.

Project mapping: item 1 is Inspect → Orbit, item 2 is Gram-style, 3a is Petri-style, 3b is Bloom-style, item 4 is Prism-style.

## The properties reframe

[Agent properties for safe interactions](https://www.cooperativeai.com/post/agent-properties-for-safe-interactions) (Tilli, Nov 2025) argues scenario simulation is weak evidence: it oversimplifies, LLM behaviour is context-dependent, and situational awareness is hard to control. The author proposes measuring the agent properties that causally drive outcomes, in five clusters — motivational drivers, modelling self and others, communication and commitments, normative intelligence, strategic intelligence — and names two eval directions we should keep separate: agent assessment (does the model have property P?) and causal testing (does varying P change the outcome?).

Three consequences for what we build:

1. Properties become the measured dimensions (judge rubrics, Scout scanners) and the things we vary. Scenarios supply coverage, not the unit of measurement.
2. Capability versus propensity: run every probe with no nudge, a mild incentive, and a direct instruction. The gap between the three is the result — and it is what Gram found mattered most.
3. Context dependence is their open question and it is measurable. Report variance of a property across contexts and counterparties, not just a mean. That is a research result automated tooling is uniquely placed to produce.

## What the multi-agent Petri extension misses

[The extension](https://www.lesswrong.com/posts/DhFgiMzWjg7XboPDz/a-multi-agent-extension-for-petri) (Cullen, July 2026) gives the auditor N target channels, adds `select_target()` and `route_message()`, supports configurable topologies and runs one judge per agent. It reports no results. In short: it makes Petri talk to several targets, but it does not yet audit a system.

Gaps, roughly by importance:

1. **The auditor is the message bus.** Every message passes through `route_message()`, so the auditor can reorder or reword it and becomes an uncontrolled participant. Add a faithful-channel mode (verbatim delivery, auditor observes only) alongside the adversarial mode, and log the raw message beside what was delivered.
2. **Judging is per agent.** Single-agent dimensions cannot capture collusion, influence, capitulation, coercion, or outcomes that are bad although no agent misbehaved. Add a system-level judge that sees the full transcript plus each agent's private view.
3. **No search over system structure.** Topology, model mix, roles, memory visibility and monitors are fixed inputs, not things the auditor can vary. This is exactly item 3a, and Orbit already has it.
4. **No single-agent baseline.** A behaviour seen in a multi-agent run may be something the model does on its own. Every run needs a comparison: the same task with one agent, with scripted peers, and with a different model mix. Without those, "the agents colluded" can just mean "the model does this anyway".
5. **Collusion needs an overseer and a covert channel.** Neither monitor roles nor private channels exist here.
6. **More agents, more tells that this is a test.** In a single-agent audit the target can only notice that its tools are fake; here it can also notice that its peers are. Every extra agent adds another chance to spot the setup, and nothing measures per-agent realism or eval awareness. Bloom scores both; Dish and Orbit offer real environments.
7. **Rollback and turn-taking are single-agent ideas.** Rolling back one agent leaves the others inconsistent, so we need joint snapshots. Turn order is sequential and auditor-chosen, so there is no asynchrony or timing, and the auditor's context grows with N, capping scale at a handful of agents.
8. **Mixed models unclear.** The post does not say whether targets can be different models. A weak model steering a strong one is a core threat model.
9. **No surrounding scaffolding.** No multi-agent seeds, no Scout scanners for multi-agent transcripts, no validity checks — items 3b and 4 of the research goals.

## What to measure — TBD

Following Tilli's post, the unit should be a property that causally drives interaction outcomes, not a catalogue of scenarios (prisoner's dilemma etc): pick the property, then build scenarios to measure it. Properties would need to be both tractable to measure and safety-relevant.

## Tooling that would help

To do.

## Open questions

- Think more about capabilities and propensities.
- Is a property-profile-per-model output in scope, or is the project strictly tooling?
- Focus on creating multi-agent specific scenario first or extending current tools to multi-agent.

## Sources

- [Inspect](https://inspect.aisi.org.uk/) · [Inspect Scout](https://meridianlabs-ai.github.io/inspect_scout/) · [Orbit](https://github.com/wlanderson0/orbit)
- [Petri](https://meridianlabs-ai.github.io/inspect_petri/) · [Petri Dish](https://meridianlabs-ai.github.io/petri_dish/) · [Bloom](https://meridianlabs-ai.github.io/petri_bloom/)
- [Gram (arXiv 2605.30322)](https://arxiv.org/abs/2605.30322) · [Prism](https://github.com/LAThomson/prism)
- [A multi-agent extension for Petri](https://www.lesswrong.com/posts/DhFgiMzWjg7XboPDz/a-multi-agent-extension-for-petri) · [Agent properties for safe interactions](https://www.cooperativeai.com/post/agent-properties-for-safe-interactions)
