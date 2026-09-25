# Orbit Spec — Outline

This is the skeleton of the spec for adding Petri-style automated auditing to Orbit.

## 1. Overview

### 1.1 Motivation

Why single-agent auditors (Petri, Bloom, etc.) miss behaviours that only emerge from interactions between agents, and why Orbit is the right base.

### 1.2 Goal and research question

What the auditor should do: given a scenario description, try hard to elicit a target multi-agent behaviour, including by searching over configurations, roles and settings.

### 1.3 Scope and non-goals

What this spec covers now versus later, and what is explicitly out of scope.

- Evaluation awareness is out of scope; we mitigate by starting with weaker models and optionally checking verbalised eval awareness in open-weight models.
- Covert communication channel is out-of-scope.

### 1.4 (Optional) Terminology

Short glossary (auditor, target, timestep, checkpoint, fork, seed, dimension) so we all use the same words.

## 2. How this differs from Multi-Agent Petri

Comparison with [Petri_MultiAgent](https://github.com/carissacullen/Petri_MultiAgent_/tree/main) ([write-up](https://www.lesswrong.com/posts/DhFgiMzWjg7XboPDz/a-multi-agent-extension-for-petri)).

- What's missing and why we are doing this.
- Technical comparison:
  - Who generates inter-agent messages
    - Auditor-scripted interactions versus real agent-to-agent communication over Orbit channels.
  - What the auditor can control
    - Which ability each system gives the auditor (messages, tools, rollback, setup changes).
  - Implications for validity
    - How much each approach distorts the interaction being measured, and what that means for interpreting results.

## 3. Core concepts

### 3.1 Audit setup and seed instructions

What a multi-agent seed looks like (scenario, agents, roles, topology, target behaviour) and how it maps to an Orbit config.

### 3.2 Turn cycle and timesteps

Defines what one timestep is when several agents are running and at which points the auditor can step in.

### 3.3 Auditor role

What the auditor sees, what it can do, and its budget and stopping criteria.

### 3.4 Target and non-target agents

Which agents are under evaluation, which are scripted or auditor-controllable, and what each can see.

### 3.5 Tools: real versus simulated

When tool results are real Orbit tools and when the auditor simulates them.

### 3.6 Transcript

How a multi-agent, possibly branched run is recorded so the judge can cite evidence.

### 3.7 Checkpoints, rollback and forking

How eval state is saved, restored and branched, including when real tools have side effects.

## 4. Auditor levels

The levels of auditor following John's doc.

### 4.1 Level 0: auditor as an agent in the eval

The auditor is a peer inside the scenario; possibly subsumed by Level 1.

### 4.2 Level 1: auditor outside the eval, acting on it

Petri's affordances plus taking over or spoofing any agent, with rollback.

### 4.3 Level 2: auditor changing the eval setup

The auditor searches over configurations, roles, topologies and mid-sample changes (e.g. adding an agent) rather than editing messages.

### 4.4 Hybrid

Whether and how one auditor combines Level 1 and Level 2 actions.

## 5. Auditor tooling and interfaces

The programmatic objects and tools the auditor uses to inspect and act on an Orbit run.

### 5.1 Transcript object

A compact representation of the run that the auditor can query and edit in code.

- Must support: loading by timestep range, advancing one timestep, stepping back, editing portions, and running subagents on portions.

### 5.2 Config object

A representation of the scenario config the auditor can modify without breaking the run.

- Must support enabling or disabling conversations, i.e. changing connectivity in the communication graph.

### 5.3 On-the-fly versus start-only changes

Which config changes can be applied mid-sample (as a fork) and which require a fresh run.

### 5.4 How the auditor perceives state

What format and level of detail the auditor receives, and whether it explores state agentically or gets it as a message.

### 5.5 Auditor tool list

The concrete tool calls (e.g. send as agent, simulate tool, rollback, fork, amend config, end audit) with signatures.

## 6. Judging and scoring

How a judge scores multi-agent transcripts, including across branches and forked runs.

### 6.1 Behaviour dimensions

The per-property rubrics the judge scores against.

### 6.2 Audit-quality dimensions

How much the auditor "put its thumb on the scale" and whether the scenario stayed realistic.

### 6.3 Aggregation across runs

How scores combine across seeds, forks and Level 2 configuration sweeps.

## 7. Target properties

We start by testing for the following properties:

- Memetic spread: how far an idea, belief or behaviour introduced by one agent propagates through the population via inter-agent communication.
- Persuasiveness: whether an agent can influence the beliefs, preferences or intended actions of other agents through communication or framing ([Tili's post](https://www.cooperativeai.com/post/agent-properties-for-safe-interactions)).
- Susceptibility to persuasion: how easily an agent's beliefs, preferences or actions are shifted by other agents, the receiving side of persuasiveness and related to exploitability.

### 7.1 Seed scenarios per property

The starter seeds and expected signals for each of the three properties.

Following Tili's post we want to consider multiple scenario for each property.

## 8. Validation

How we know results are real: baselines, sanity checks, human review, and robustness to auditor intervention.

### 8.1 Models

Which models we run as auditor, judge and targets.

- Start with weaker models.

## 9. Comparison with other eval frameworks

A short table positioning this work against [Petri](https://meridianlabs-ai.github.io/inspect_petri/), [Petri Dish](https://meridianlabs-ai.github.io/petri_dish/), [Bloom](https://meridianlabs-ai.github.io/petri_bloom/), [Inspect Scout](https://meridianlabs-ai.github.io/inspect_scout/), [Gram](https://arxiv.org/abs/2605.30322) and [Prism](https://github.com/LAThomson/prism) (single vs multi-agent, who drives the interaction, what is searched over).

## 10. (Optional) Open questions

## 11. References

- [Orbit](https://github.com/wlanderson0/orbit) · [Inspect](https://inspect.aisi.org.uk/)
- [Petri concepts](https://meridianlabs-ai.github.io/inspect_petri/using/concepts.html)
- [A multi-agent extension for Petri](https://www.lesswrong.com/posts/DhFgiMzWjg7XboPDz/a-multi-agent-extension-for-petri) · [Petri_MultiAgent repo](https://github.com/carissacullen/Petri_MultiAgent_/tree/main)
- [Agent properties for safe interactions](https://www.cooperativeai.com/post/agent-properties-for-safe-interactions)
