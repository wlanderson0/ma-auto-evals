"""A scenario's tool ends the run early with ``end_run`` (spec ``docs/level2-interface.md``
§3.5), under the audit runtime.

Three rounds of alice, bob, carol, dave (12 turns). Bob's first generation calls
``agree``, which calls ``end_run``: the run stops after his turn, with no error, and the
sample's store says why. A rollback to before Bob's first generation, replacing it with
a post, gives a branch whose store starts clean and that runs the whole schedule.
"""

from __future__ import annotations

from inspect_ai import eval as inspect_eval
from inspect_ai.model import ChatMessage, ModelOutput, get_model
from inspect_ai.tool import Tool, tool
from orbit.ids import id_scope, sequential_ids

from experiments.scenarios import RUN_END_KEY, RunEnd, build_team, end_run, team_setup
from orbit_audit.boundaries import Boundary
from orbit_audit.branches import BranchStatus
from orbit_audit.ops import ReplaceGeneration, Rollback, ToolCallSpec
from orbit_audit.policy import AuditPolicy
from orbit_audit.runner import BRANCHES_KEY
from orbit_audit.task import audit_task
from tests.support.runs import model_events, sample
from tests.support.scripted import call, post, turns_taken

TEAM = ("alice", "bob", "carol", "dave")
REASON = "deal passed: bob agreed"


@tool
def agree(agent: str) -> Tool:
    async def execute() -> str:
        """Agree to the deal on the table."""
        end_run(REASON)
        end_run("a second reason is ignored")
        return "Agreed."

    return execute


def speaker(messages: list[ChatMessage]) -> str:
    system = next(m.text for m in messages if m.role == "system")
    return next(agent for agent in TEAM if system.startswith(f"You are {agent} at "))


def models(messages, tools, tool_choice, config) -> ModelOutput:
    agent = speaker(messages)
    if agent == "bob" and turns_taken(messages) == 0:
        return call("agree")
    return post(f"{agent} here", "town")


def run(tmp_path, policy):
    team = team_setup({}, personas={agent: agent for agent in TEAM}, tools={"agree": agree})
    controllers = []
    task = audit_task(policy=lambda: policy, on_controller=controllers.append,
                      tool_replay={"agree": "pure"},  # its effect lives in the branch's store
                      **build_team("deal", team))
    model = get_model("mockllm/model", custom_outputs=models, memoize=False)
    with id_scope(sequential_ids()):
        [log] = inspect_eval(task, model=model, log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.samples[0].error
    return log, controllers


def speakers(log) -> list[str]:
    return [speaker(e.input) for e in model_events(log)]


def test_end_run_stops_the_schedule_after_the_turn_that_called_it(tmp_path):
    log, _ = run(tmp_path, AuditPolicy())
    assert speakers(log) == ["alice", "bob"]
    assert sample(log).store[RUN_END_KEY] == REASON  # the first reason is kept
    assert [n["status"] for n in sample(log).store[BRANCHES_KEY]] == ["completed"]
    halted = [e.data for e in sample(log).events
              if e.event == "info" and "halt_reason=" in str(e.data)]
    assert halted and "halt_reason=custom:run_ended" in str(halted[-1])


class RollBackTheDeal(AuditPolicy):
    """After the deal, roll back to Bob's first generation and post instead."""

    name = "roll-back-the-deal"

    def __init__(self):
        self.fork: int | None = None

    async def decide(self, view):
        if view.branch != "branch-0":
            return []
        if (view.boundary, view.agent, view.generation) == (Boundary.GENERATION, "bob", 0):
            self.fork = view.revision
        if (view.boundary, view.turn) == (Boundary.BETWEEN_TURNS, 1):
            return [Rollback(to_revision=self.fork, reason="no deal", interventions=(
                ReplaceGeneration(agent="bob", tool_calls=(ToolCallSpec(
                    function="channel_post",
                    arguments={"channel_id": "town", "message": "not yet"}),)),))]
        return []


def test_a_rollback_from_a_run_that_ended_early_runs_on_with_a_clean_store(tmp_path):
    log, (parent, child) = run(tmp_path, RollBackTheDeal())
    tree = sample(log).store[BRANCHES_KEY]
    assert [(n["id"], n["status"]) for n in tree] == [
        ("branch-0", BranchStatus.ROLLED_BACK.value), ("branch-1", BranchStatus.COMPLETED.value)]
    assert RUN_END_KEY not in sample(log).store
    assert [g.agent for g in child.generations] == list(TEAM) * 3
    assert RunEnd(store=parent.runtime.state.store).reason == REASON
