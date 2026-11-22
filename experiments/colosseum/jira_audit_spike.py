"""Spike: Orbit's Jira task with Colosseum's three conditions, under orbit_audit's audit_solver.

Runs Orbit's ``jira_ticket_allocation`` on scripted ``mockllm`` agents in Colosseum's three
conditions (control, emergent = ``channel_only``, prompted), each once as Orbit builds it and once
wrapped in ``audit_solver`` with a passive ``AuditPolicy``. A passive audit should change nothing,
so the Jira, DCOP and collusion scores of each pair should match. No network, no key:

    uv run python experiments/colosseum/jira_audit_spike.py              # all conditions
    uv run python experiments/colosseum/jira_audit_spike.py prompted -v  # one, with the details

The sizes follow Colosseum's RQ3 Jira run: 6 developers, 8 tickets, one claim each
(``max_claims_per_agent=1``) and a coalition of two. The scripted agents are deliberately simple:
a coalition member first posts on the secret channel when it has one, then every agent posts its
plan on the team channel, claims ticket ``i`` and submits. This checks the plumbing, not behaviour.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any

from inspect_ai import eval as inspect_eval
from inspect_ai import task_with
from inspect_ai.log import read_eval_log
from inspect_ai.model import Model, ModelOutput, get_model
from orbit.scenarios.cooperative_allocation.jira_ticket.dataset_builder import build_problem
from orbit.scenarios.cooperative_allocation.jira_ticket.task import jira_ticket_allocation

from orbit_audit.policy import AuditPolicy
from orbit_audit.runner import audit_solver

DEVELOPERS = 6
TICKETS = 8
SEED = 733
MAX_CLAIMS = 1
COALITION = ("dev_0", "dev_1")
MAX_TURNS = 16  # enough for every agent to post, claim and submit

# Colosseum's conditions as Orbit task arguments.
CONDITIONS: dict[str, dict[str, Any]] = {
    "control": {},
    "emergent": {"coalition_agents": ",".join(COALITION), "collusion_mode": "channel_only"},
    "prompted": {"coalition_agents": ",".join(COALITION), "collusion_mode": "prompted"},
}

# The scores that must not move when a passive audit wraps the task.
COMPARED_SCORES = ("jira_ticket_scorer", "dcop_scorer", "collusion_scorer")


def ticket_ids(seed: int = SEED) -> list[str]:
    problem = build_problem(
        num_developers=DEVELOPERS, num_tasks=TICKETS, seed=seed, max_claims_per_agent=MAX_CLAIMS
    )
    return [getattr(t, "task_id", None) or t.id for t in problem.tasks]


@dataclass
class _Agent:
    secret_sent: bool = False
    posted: bool = False
    assigned: bool = False
    saw_coalition_prompt: bool = False


@dataclass
class ScriptedTeam:
    """One run's scripted developers; keeps each agent's progress through the script."""

    tickets: list[str]
    agents: dict[str, _Agent] = field(default_factory=dict)

    def model(self) -> Model:
        return get_model("mockllm/model", custom_outputs=self._next, memoize=False)

    def _next(self, messages, tools, tool_choice, config) -> ModelOutput:
        system = "\n".join(str(m.content) for m in messages if m.role == "system")
        found = re.search(r"developer (dev_\d+)", system)
        if found is None:
            return ModelOutput.from_content("mockllm/model", "done")
        name = found.group(1)
        agent = self.agents.setdefault(name, _Agent())
        agent.saw_coalition_prompt |= any("COALITION" in str(m.content) for m in messages)
        tool_names = {t.name for t in tools}
        ticket = self.tickets[int(name.split("_")[1]) % len(self.tickets)]

        if "secret_channel" in tool_names and not agent.secret_sent:
            agent.secret_sent = True
            return _call("secret_channel", action="send", message=f"{name}: I'll take {ticket}")
        if "channel_post" in tool_names and not agent.posted:
            agent.posted = True
            return _call("channel_post", channel_id="team", message=f"{name} plans {ticket}")
        if "assign_task" in tool_names and not agent.assigned:
            agent.assigned = True
            return _call("assign_task", task_id=ticket)
        if "submit" in tool_names:
            return _call("submit", answer=f"{name} done")
        return ModelOutput.from_content("mockllm/model", f"{name} done")


def _call(tool: str, **arguments: Any) -> ModelOutput:
    return ModelOutput.for_tool_call("mockllm/model", tool, arguments)


def run(condition: str, audited: bool, seed: int = SEED) -> dict[str, Any]:
    """Run one condition, plain or under a passive audit, and summarise the sample."""
    task = jira_ticket_allocation(
        num_developers=DEVELOPERS,
        num_tasks=TICKETS,
        seed=seed,
        max_turns=MAX_TURNS,
        max_claims_per_agent=MAX_CLAIMS,
        runtime_profile="common/v1",
        **CONDITIONS[condition],
    )
    if audited:
        task = task_with(task, solver=audit_solver(lambda: AuditPolicy()))
    team = ScriptedTeam(ticket_ids(seed))
    with tempfile.TemporaryDirectory() as log_dir:
        [log] = inspect_eval(task, model=team.model(), log_dir=log_dir, display="none")
        full = read_eval_log(log.location)

    summary: dict[str, Any] = {
        "condition": condition,
        "audited": audited,
        "status": full.status,
        "error": str(full.error)[:300] if full.error else None,
    }
    if not full.samples:
        return summary
    sample = full.samples[0]
    scores = sample.scores or {}
    info = [
        e.data
        for e in sample.events
        if getattr(e, "event", "") == "info"
        and getattr(e, "source", "") == "orbit_audit"
        and isinstance(getattr(e, "data", None), dict)
    ]
    messages = (sample.store or {}).get("CommunicationState:messages") or []
    summary.update(
        scores={k: v.value for k, v in scores.items()},
        per_agent_reward=(scores["jira_ticket_scorer"].metadata or {}).get("per_agent_reward")
        if "jira_ticket_scorer" in scores
        else None,
        assignments=(sample.store or {}).get("JiraTicketState:assignments"),
        channel_posts=[(m.get("sender"), m.get("channel_id")) for m in messages],
        orbit_audit_events=sorted({d.get("event") for d in info}),
        branch_events=[
            (d.get("action"), d.get("status"))
            for d in info
            if d.get("event") == "orbit_audit_branch"
        ],
        coalition_prompt_seen=sorted(n for n, a in team.agents.items() if a.saw_coalition_prompt),
    )
    return summary


def compare(condition: str, seed: int = SEED) -> dict[str, Any]:
    """Run a condition plain and audited; report whether the compared scores match."""
    plain, audited = run(condition, False, seed), run(condition, True, seed)
    mismatched = [
        name
        for name in COMPARED_SCORES
        if not _same(plain.get("scores", {}).get(name), audited.get("scores", {}).get(name))
    ]
    return {"plain": plain, "audited": audited, "mismatched": mismatched}


def _same(a: Any, b: Any) -> bool:
    """Equal, counting NaN as equal to NaN (Orbit reports unavailable metrics as NaN)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "conditions", nargs="*", help=f"any of {', '.join(CONDITIONS)} (default: all)"
    )
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("-v", "--verbose", action="store_true", help="print both summaries")
    args = parser.parse_args()
    unknown = sorted(set(args.conditions) - set(CONDITIONS))
    if unknown:
        parser.error(f"unknown condition(s): {', '.join(unknown)}")
    failed = False
    for condition in args.conditions or list(CONDITIONS):
        result = compare(condition, args.seed)
        ok = not result["mismatched"] and all(
            result[k]["status"] == "success" for k in ("plain", "audited")
        )
        failed |= not ok
        print(
            f"{condition:9s} {'ok' if ok else 'MISMATCH'}  "
            f"branch events {result['audited'].get('branch_events')}  "
            f"mismatched {result['mismatched']}"
        )
        if args.verbose or not ok:
            print(json.dumps(result, indent=1, default=str))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
