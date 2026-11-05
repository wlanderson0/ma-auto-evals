"""What L2 is shown about a scenario before it designs variants (plan
``docs/level2-milestone3.md``, "What L2 sees"): everything, as a human researcher
would see it. Its agents with their models and prompts, channels with who reads and
writes them, the schedule, the seat, the targets and the scorers.
"""

from __future__ import annotations

from experiments.l2 import written as written_
from experiments.l2.experiment import base_scenario, models_for
from experiments.scenarios import Scenario
from experiments.scorers import DOSE_BASES


def describe_scenario(name: str, mock: bool,
                      written: written_.WrittenScenarios = written_.NONE) -> str:
    """``name``'s setup as it runs (on the mock models if ``mock``), in Markdown; ``name``
    may be one of the ``written`` scenarios."""
    scenario = base_scenario(name, mock, written)
    task = scenario.task
    lines = [f"## {name} ({scenario.property})", ""]
    if scenario.team is None:
        lines += ["Not parameterised yet: no ready-made change applies to it.", ""]
    lines += [f"- Seat: {scenario.seat}. Targets (measured): {', '.join(scenario.targets)}.",
              f"- Task, given to every agent: {task['dataset'][0].input}",
              f"- Schedule: {schedule(scenario)}", "- Channels:"]
    lines += [f"  - #{c.id}: read by {', '.join(c.readers)}; written by {', '.join(c.writers)}"
              for c in task["communication"].channels]
    lines.append("- Agents (model; tools; system prompt):")
    for spec in task["setup"].agents:
        tools = ", ".join(["channel_post", *(str(t) for t in spec.tools)])
        model = spec.model or ("the run's default model" if mock
                               else f"the run's default model, {models_for(False)['target']}")
        lines.append(f"  - {spec.name} ({model}; {tools}): {spec.system_prompt}")
    lines += ["- Scorers (all run after the agents have finished):", *scorers(scenario)]
    return "\n".join(lines)


def schedule(scenario: Scenario) -> str:
    steps = [", ".join(step.agents) for step in scenario.task["execution"].plan.steps]
    team = scenario.team
    if team is not None and team.rounds is not None:
        per_round = len(steps) // team.rounds
        text = f"{team.rounds} rounds, each: {'; '.join(steps[:per_round])}"
    else:
        text = f"{len(steps)} steps: {'; '.join(steps)}"
    output = scenario.task["execution"].output_agent
    idle = [s.name for s in scenario.task["setup"].agents
            if not any(s.name in step.agents for step in scenario.task["execution"].plan.steps)]
    return (text + (f". Agents with no turns: {', '.join(idle)}" if idle else "")
            + (f". {output}'s final reply is the run's output, so it must stay scheduled"
               if output else ""))


def scorers(scenario: Scenario) -> list[str]:
    lines = []
    questions: dict[str, list[str]] = {}
    for agent, probe in scenario.probes.items():
        text = (f"{probe.asked} (options {', '.join(probe.options)}; {probe.adopted} "
                "counts as adopting)")
        questions.setdefault(text, []).append(agent)
    lines += [f"  - probe, asked privately of {', '.join(agents)}: {text}"
              for text, agents in questions.items()]
    lines.append(f"  - post_judge: a judge asks of each target post: {scenario.judge}")
    if scenario.mention:
        lines.append(f"  - counts and round_counts: uses of /{scenario.mention}/ (case "
                     "insensitive) in each target's own posts, in all and per round")
    lines += [f"  - free probe {label!r}, asked privately of each target: {question}"
              for label, question in scenario.free_probes.items()]
    if scenario.outcome is not None:
        lines.append(f"  - outcome ({scenario.outcome.__name__}): what each target did with "
                     "its tools, in the log's scores")
    if scenario.base in DOSE_BASES:
        lines.append("  - seat_dose: the dose the seat delivered in its own posts, against "
                     "the dose a persona/append_line gave it")
    return lines
