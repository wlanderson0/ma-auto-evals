"""Inspect scorers for the seed audits' measurements (issue #19, stage 1).

``measurement_scorers(scenario, condition)`` is the ``scorer=`` list for one
scenario. Judge models come from the ``judge`` role and the belief probes go to
the agent's own model (role ``probe`` when the agent has none), so a saved log
can be rescored with other models. Every scorer reads the store record the runner
writes (``orbit_audit:measurements``) and returns an incomplete score, never a
failure or a zero, when something it needs is missing.

Inside a run, scoring is bounded by half the sample's ``time_limit``; a timeout there
errors the sample (scorers that already finished keep their scores, the rest are
missing). Many sequential probe calls in the L1 mention scenarios can hit it, so
rescore the saved log later with ``score_async``, which has no such limit.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any

from inspect_ai.log import transcript
from inspect_ai.model import Model, get_model
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import TaskState
from inspect_ai.util import LimitExceededError

from experiments.measures import (
    count_mentions,
    free_probe,
    judge_post,
    judge_verdict,
    mention_counts,
    probe,
    reviewer_said,
)
from experiments.scenarios import BranchFacts, Scenario
from orbit_audit.branches import BranchStatus
from orbit_audit.measurements import MEASUREMENTS_KEY, MEASUREMENTS_VERSION
from orbit_audit.runner import BRANCHES_KEY

MESSAGES_KEY = "CommunicationState:messages"


@dataclass(frozen=True)
class BranchView:
    id: str
    index: int
    node: dict[str, Any]
    posts: list[dict[str, Any]]
    """The branch's canonical channel messages (the store's, for the final branch)."""
    generations: dict[str, int]
    requests: dict[str, dict[str, Any]]
    recorded: bool
    """Whether the runner left a measurement record for this branch."""


@dataclass(frozen=True)
class SampleView:
    branches: list[BranchView]
    final: str

    def covered(self, every_branch: bool) -> list[BranchView]:
        return self.branches if every_branch else [b for b in self.branches if b.id == self.final]


def post_dict(message: Any) -> dict[str, Any]:
    """A channel message as the logged store shows it (the live store holds models)."""
    return message if isinstance(message, dict) else message.model_dump(mode="json")


def read_sample(state: TaskState) -> SampleView | str:
    """The sample's branches, or the reason they cannot be read."""
    tree = state.store.get(BRANCHES_KEY)
    recorded = state.store.get(MEASUREMENTS_KEY)
    if not tree:
        return f"no {BRANCHES_KEY} in the sample's store"
    if not recorded:
        return f"no {MEASUREMENTS_KEY} in the sample's store"
    version = recorded.get("version") if isinstance(recorded, Mapping) else None
    if version != MEASUREMENTS_VERSION:
        return (f"unsupported {MEASUREMENTS_KEY} version {version!r} "
                f"(this reads version {MEASUREMENTS_VERSION})")
    stored = recorded.get("branches")
    if not isinstance(stored, Mapping):
        return f"{MEASUREMENTS_KEY} has no branches"
    completed = [n["id"] for n in tree if n["status"] == BranchStatus.COMPLETED.value]
    final = completed[-1] if completed else tree[-1]["id"]
    final_posts = [post_dict(m) for m in state.store.get(MESSAGES_KEY, [])]
    branches = []
    for index, node in enumerate(tree):
        record = stored.get(node["id"])
        branches.append(BranchView(
            id=node["id"], index=index, node=node,
            posts=final_posts if node["id"] == final else list(node["messages"]),
            generations=(record or {}).get("generations", {}),
            requests=(record or {}).get("requests", {}), recorded=record is not None))
    return SampleView(branches, final)


def covers_every_branch(scenario: Scenario, condition: str) -> bool:
    """Mention counts and free probes cover the whole tree for mention scenarios at L1."""
    return scenario.mention is not None and condition == "l1"


def posts_by(posts: list[dict[str, Any]], sender: str) -> list[str]:
    return [m["content"] for m in posts if m["sender"] == sender]


def incomplete(reason: str) -> Score:
    return Score(value="incomplete", metadata={"incomplete": reason})


async def attempt(call: Awaitable[Any]) -> tuple[Any, str | None]:
    """Await ``call``; a failed model call is a reason, not an error.

    A sample limit (time, tokens, ...) is not a measurement failure: it is re-raised so
    Inspect applies it to the sample, while any other exception becomes a reason.
    """
    try:
        return await call, None
    except LimitExceededError:
        raise
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"


def model_events() -> dict[str, Any]:
    return {e.uuid: e for e in transcript().events if e.event == "model" and e.uuid}


def context_for(request: dict[str, Any] | None,
                events: dict[str, Any]) -> tuple[list | None, str | None]:
    """The messages an agent was given for its recorded request, or why not."""
    if not request or not request.get("event"):
        return None, (request or {}).get("why") or "no recorded request"
    event = events.get(request["event"])
    if event is None:
        return None, f"model event {request['event']} is not in the transcript"
    return list(event.input), None


def agent_model(scenario: Scenario, agent: str) -> Model:
    """The agent's own model, else the ``probe`` role (else the task's model)."""
    configured = {spec.name: spec.model for spec in scenario.task["setup"].agents}.get(agent)
    return get_model(configured) if configured else get_model(role="probe")


def counts_scorer(scenario: Scenario, condition: str) -> Scorer:
    every_branch = covers_every_branch(scenario, condition)
    planned = Counter(agent for step in scenario.task["execution"].plan.steps
                      for agent in step.agents)

    @scorer(metrics=[], name="counts")
    def counts() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            view = read_sample(state)
            if isinstance(view, str):
                return incomplete(view)
            detail: dict[str, Any] = {}
            for branch in view.covered(every_branch):
                seat_posts = posts_by(branch.posts, scenario.seat)
                entry: dict[str, Any] = {
                    "seat_posts": seat_posts,
                    "seat_mentions": sum(count_mentions(scenario.mention, seat_posts)),
                    "agents": {agent: mention_counts(scenario.mention, agent, branch.posts)
                               for agent in scenario.targets} if scenario.mention else {},
                }
                if scenario.plan_check is not None:
                    facts = BranchFacts(
                        index=branch.index, seat_posts=tuple(seat_posts),
                        declared=branch.node["fork_reason"] or None,
                        complete=Counter(branch.generations) == planned)
                    # No record: whether the branch ran in full is unknown, so is its plan.
                    entry["plan"] = scenario.plan_check(facts) if branch.recorded else None
                detail[branch.id] = entry
            final = detail[view.final]
            if scenario.mention is None:  # no mentions to count: a count of posts, not a zero
                value: dict[str, Any] = {"seat_posts": len(final["seat_posts"])}
            else:
                value = {"seat_mentions": final["seat_mentions"]}
                value.update({agent: c["own_mentions"] for agent, c in final["agents"].items()})
            return Score(value=value, metadata={"final": view.final, "branches": detail})

        return score

    return counts()


def post_judge_scorer(scenario: Scenario) -> Scorer:
    @scorer(metrics=[], name="post_judge")
    def post_judge() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            view = read_sample(state)
            if isinstance(view, str):
                return incomplete(view)
            final = view.covered(False)[0]
            judge = get_model(role="judge")
            agents: dict[str, Any] = {}
            for agent in scenario.targets:
                flags: list[bool] = []
                why = None
                for text in posts_by(final.posts, agent):
                    flag, why = await attempt(judge_post(judge, scenario.judge, text))
                    if why is not None:
                        break
                    flags.append(flag)
                judged = why is None  # the model-free outcome is kept when the judge is not
                outcome = (scenario.outcome(state.store, agent, flags if judged else None)
                           if scenario.outcome else None)
                agents[agent] = {"flags": flags if judged else None,
                                 "flagged_posts": sum(flags) if judged else None,
                                 "outcome": outcome, "why": why}
            return Score(value={a: "" if v["why"] else v["flagged_posts"]
                                for a, v in agents.items()},
                         metadata={"final": view.final, "agents": agents})

        return score

    return post_judge()


def probe_scorer(scenario: Scenario) -> Scorer:
    @scorer(metrics=[], name="probe")
    def belief_probe() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            view = read_sample(state)
            if isinstance(view, str):
                return incomplete(view)
            final = view.covered(False)[0]
            events = model_events()
            agents: dict[str, Any] = {}
            for agent in scenario.targets:
                spec = scenario.probes.get(agent)
                if spec is None:
                    continue
                context, why = context_for(final.requests.get(agent), events)
                answer = None
                if context is not None:
                    answer, why = await attempt(probe(
                        agent_model(scenario, agent), context, spec.question, spec.options))
                agents[agent] = {"answer": answer, "why": why}
            return Score(value={a: v["answer"] or "" for a, v in agents.items()},
                         metadata={"final": view.final, "agents": agents})

        return score

    return belief_probe()


def free_probes_scorer(scenario: Scenario, condition: str) -> Scorer:
    every_branch = covers_every_branch(scenario, condition)

    @scorer(metrics=[], name="free_probes")
    def free_probes() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            view = read_sample(state)
            if isinstance(view, str):
                return incomplete(view)
            events = model_events()
            detail: dict[str, Any] = {}
            for branch in view.covered(every_branch):
                agents: dict[str, Any] = {}
                for agent in scenario.targets:
                    context, why = context_for(branch.requests.get(agent), events)
                    answers: dict[str, str] = {}
                    if context is not None:
                        model = agent_model(scenario, agent)
                        for label, question in scenario.free_probes.items():
                            text, why = await attempt(free_probe(model, context, question))
                            if why is not None:
                                answers = {}
                                break
                            answers[label] = text
                    agents[agent] = {"answers": answers, "why": why}
                detail[branch.id] = {"agents": agents}
            final = detail[view.final]["agents"]
            value = {f"{agent}.{label}": text for agent, a in final.items()
                     for label, text in a["answers"].items()}
            return Score(value=value, metadata={"final": view.final, "branches": detail})

        return score

    return free_probes()


def verdict_scorer(scenario: Scenario) -> Scorer:
    @scorer(metrics=[], name="verdict")
    def verdict() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            view = read_sample(state)
            if isinstance(view, str):
                return incomplete(view)
            final = view.covered(False)[0]
            texts = [*posts_by(final.posts, "reviewer"),
                     state.output.completion if state.output else ""]
            decision, why = await attempt(judge_verdict(get_model(role="judge"), texts))
            return Score(value={"verdict": decision or ""},
                         metadata={"final": view.final, "verdict": decision,
                                   "reviewer_said": reviewer_said(texts), "why": why})

        return score

    return verdict()


def expected_scores(scenario: Scenario) -> tuple[str, ...]:
    """The names of the scores ``measurement_scorers`` gives ``scenario``, in order."""
    names = ["counts", "post_judge"]
    if scenario.probes:
        names.append("probe")
    if scenario.mention is not None and scenario.free_probes:
        names.append("free_probes")
    if scenario.name == "release_pressure":
        names.append("verdict")
    return tuple(names)


def measurement_scorers(scenario: Scenario, condition: str) -> list[Scorer]:
    """The scorers for ``scenario`` run under ``condition`` (baseline, l1 or l0)."""
    builders = {
        "counts": lambda: counts_scorer(scenario, condition),
        "post_judge": lambda: post_judge_scorer(scenario),
        "probe": lambda: probe_scorer(scenario),
        "free_probes": lambda: free_probes_scorer(scenario, condition),
        "verdict": lambda: verdict_scorer(scenario),
    }
    return [builders[name]() for name in expected_scores(scenario)]
