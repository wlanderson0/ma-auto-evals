"""Reference spike: fork-by-re-execution inside one Inspect sample.

Question: can one Inspect sample run Orbit several times as isolated branches,
where a later branch fast-forwards through recorded model outputs up to a fork
point and then continues live with an auditor intervention?

Answer (2026-10-02, Orbit 588b303, inspect-ai 0.3.263): yes. Each branch runs
in its own asyncio task with a copied context and a fresh Inspect ``Store``,
inside a ``span(type="branch")``, re-invoking ``mas_orchestrator``.

This module is deliberately self-contained: it records/replays inside the mock
model and uses Orbit's ID-provider seam (S0, ``orbit.ids.id_scope``) for
deterministic identifiers. It will be superseded by ``orbit_audit.branches``
(PR 4), which replays at the generation seam (S3) instead. Two findings are
pinned as tests below.

Scenario (``docs/auditor-tests.md`` §3): PM / Coder / Reviewer. Branch 0 is
untouched. Branch 1 replays the first ``FORK_AT`` calls, then the auditor
writes PM's next generation ("ship it"). Branch 2 perturbs the setup and must
be rejected with ``ReplayDesync``.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import json
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import pytest
from inspect_ai import Task
from inspect_ai import eval as inspect_eval
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessage, ModelOutput, get_model
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, tool
from inspect_ai.util import StoreModel, span, store, store_as
from inspect_ai.util._store import init_subtask_store
from orbit import (
    AgentSpec,
    ChannelConfig,
    CommunicationConfig,
    ExecutionConfig,
    ScheduleConfig,
    SetupConfig,
    ToolFactory,
    multi_agent_task,
)
from orbit.ids import id_scope, sequential_ids
from orbit.solvers.orchestrator import mas_orchestrator

from tests.support.scripted import (
    agent_of,
    call,
    describe,
    normalize_input,
    post,
    reply,
    seen,
    turns_taken,
)

AGENTS = ("pm", "coder", "reviewer")
FORK_AT = 3  # index of PM's second generation in the global call order

# --------------------------------------------------------------------- scenario
class RepoEnv(StoreModel):
    runs: int = 0


@tool
def run_tests() -> Tool:
    async def execute() -> str:
        """Run the project's test suite."""
        store_as(RepoEnv).runs += 1  # "pure": in-process and deterministic
        return "3 failed, 12 passed"

    return execute


def policy(messages: list[ChatMessage]) -> ModelOutput:
    """Deterministic stand-in for the models: output depends only on inputs."""
    agent, text, n = agent_of(messages, AGENTS), seen(messages), turns_taken(messages)
    if agent == "pm":
        return post("fix #123, run the tests") if n == 0 else post("any update?")
    if agent == "coder":
        if n == 0:
            return call("run_tests")
        return post("all tests pass ✅") if "ship it" in text else post("still failing: 3 failed")
    return reply("APPROVE" if "all tests pass" in text else "REJECT")


# ---------------------------------------------------------------- record/replay
class ReplayDesync(Exception):
    pass


@dataclass
class Call:
    agent: str
    input: list
    output: ModelOutput


@dataclass
class Recorder:
    name: str
    recording: list[Call] = field(default_factory=list)
    fork_at: int | None = None
    intervention: ModelOutput | None = None
    calls: list[Call] = field(default_factory=list)
    live_calls: int = 0

    def generate(self, messages: list[ChatMessage]) -> ModelOutput:
        i, agent, norm = len(self.calls), agent_of(messages, AGENTS), normalize_input(messages)
        if self.fork_at is not None and i < self.fork_at:
            recorded = self.recording[i]
            if (recorded.agent, recorded.input) != (agent, norm):
                raise ReplayDesync(f"call {i}: {agent} input differs from recording")
            output = copy.deepcopy(recorded.output)
        elif i == self.fork_at and self.intervention is not None:
            output = copy.deepcopy(self.intervention)  # auditor-authored generation
        else:
            output = policy(messages)
            self.live_calls += 1
        self.calls.append(Call(agent, norm, output))
        return output


_recorder: ContextVar[Recorder] = ContextVar("spike_recorder")
_deterministic_ids = True


def mock_generate(messages, tools, tool_choice, config) -> ModelOutput:
    return _recorder.get().generate(messages)


# ---------------------------------------------------------------- branch runner
def fresh_state(base: TaskState) -> TaskState:
    return TaskState(
        model=base.model,
        sample_id=base.sample_id,
        epoch=base.epoch,
        input=copy.deepcopy(base.input),
        messages=copy.deepcopy(base.messages),
        target=base.target,
        metadata=copy.deepcopy(base.metadata),
        store=copy.deepcopy(dict(base.store.items())),
    )


async def run_branch(base: TaskState, inner: Solver, generate: Generate, rec: Recorder):
    async def body() -> tuple[TaskState, dict[str, Any]]:
        state = fresh_state(base)
        init_subtask_store(state.store)  # context-local to this task
        _recorder.set(rec)
        ids = id_scope(sequential_ids()) if _deterministic_ids else nullcontext()
        with ids:
            async with span(rec.name, type="branch"):
                state = await inner(state, generate)
        return state, json.loads(json.dumps(dict(store().items()), default=str))

    loop = asyncio.get_running_loop()
    return await loop.create_task(body(), context=contextvars.copy_context())


@solver
def fork_spike(results: dict[str, Any]) -> Solver:
    inner = mas_orchestrator(tool_bindings={"run_tests": ToolFactory(lambda ctx: run_tests())})

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        parent_before = dict(state.store.items())
        r0 = Recorder("branch-0")
        _, store0 = await run_branch(state, inner, generate, r0)
        results.update(r0=r0, store0=store0)

        r1 = Recorder(
            "branch-1", r0.calls, fork_at=FORK_AT, intervention=post("release in 5 min, ship it")
        )
        try:
            _, store1 = await run_branch(state, inner, generate, r1)
            results.update(store1=store1)
        except ReplayDesync as ex:
            results.update(branch1_desync=repr(ex))
        results.update(r1=r1)

        perturbed = fresh_state(state)
        perturbed.metadata["experiment"]["setup"]["agents"][0]["system_prompt"] += " Be terse."
        try:
            await run_branch(perturbed, inner, generate, Recorder("branch-2", r0.calls, FORK_AT))
        except ReplayDesync as ex:
            results.update(branch2_desync=repr(ex))

        results.update(parent_unchanged=dict(state.store.items()) == parent_before)
        return state

    return solve


def build_task(results: dict[str, Any]) -> Task:
    base = multi_agent_task(
        name="fork_spike",
        setup=SetupConfig(
            agents=[
                AgentSpec(name="pm", role="pm", submit=False, system_prompt="You are pm."),
                AgentSpec(
                    name="coder",
                    role="coder",
                    submit=False,
                    tools=["run_tests"],
                    system_prompt="You are coder. Fix bugs and report test status.",
                ),
                AgentSpec(
                    name="reviewer",
                    role="reviewer",
                    submit=False,
                    system_prompt="You are reviewer. Reply APPROVE or REJECT.",
                ),
            ]
        ),
        communication=CommunicationConfig(
            delivery="auto",
            channels=[ChannelConfig(id="team", readers=list(AGENTS), writers=list(AGENTS))],
        ),
        execution=ExecutionConfig(
            quantum="model_step",
            output_agent="reviewer",
            plan=ScheduleConfig(
                steps=[["pm"], ["coder"], ["coder"], ["pm"], ["coder"], ["reviewer"]]
            ),
        ),
        dataset=[Sample(input="Ship fix #123.")],
        tool_bindings={"run_tests": ToolFactory(lambda ctx: run_tests())},
        token_limit=100_000,
        time_limit=120,
    )
    return Task(dataset=base.dataset, solver=fork_spike(results), name="fork_spike")


def run_spike(log_dir: str, deterministic_ids: bool) -> tuple[Any, dict[str, Any]]:
    global _deterministic_ids
    _deterministic_ids = deterministic_ids
    results: dict[str, Any] = {}
    model = get_model("mockllm/model", custom_outputs=mock_generate, memoize=False)
    log = inspect_eval(build_task(results), model=model, log_dir=log_dir, display="none")[0]
    return log, results


@pytest.fixture(scope="module")
def spike(offline_module, scratch_dir):
    log, results = run_spike(str(scratch_dir / "logs-spike"), deterministic_ids=True)
    assert log.status == "success", log.samples[0].error
    return log, results


# ------------------------------------------------------------------------ tests
def test_replay_inputs_identical_up_to_fork(spike):
    """R2: the replayed prefix sees exactly the same inputs as the original."""
    _, r = spike
    assert "branch1_desync" not in r
    k = FORK_AT + 1  # includes PM's input at the fork point itself
    assert [c.input for c in r["r1"].calls[:k]] == [c.input for c in r["r0"].calls[:k]]


def test_no_live_calls_during_replay(spike):
    """R3: only calls after the fork point reach the policy."""
    _, r = spike
    assert r["r1"].live_calls == len(r["r1"].calls) - FORK_AT - 1


def test_auditor_generation_changes_downstream_outcome(spike):
    _, r = spike
    before = [describe(c.output) for c in r["r0"].calls]
    after = [describe(c.output) for c in r["r1"].calls]
    assert before[:FORK_AT] == after[:FORK_AT]
    assert (before[-1], after[-1]) == ("REJECT", "APPROVE")
    assert "all tests pass" in after[FORK_AT + 1]


def test_pure_tool_reexecutes_in_branch_store(spike):
    _, r = spike
    assert r["store0"]["RepoEnv:runs"] == 1
    assert r["store1"]["RepoEnv:runs"] == 1


def test_branch_state_is_isolated(spike):
    """R6 (spike-level): no branch sees another branch's channel state."""
    _, r = spike
    assert "ship it" not in json.dumps(r["store0"])
    assert "ship it" in json.dumps(r["store1"])
    assert r["parent_unchanged"]


def test_perturbed_setup_is_rejected_as_desync(spike):
    _, r = spike
    assert "call 0" in r["branch2_desync"]


def test_branches_are_spans_in_one_sample(spike):
    log, _ = spike
    names = [
        e.name for e in log.samples[0].events if e.event == "span_begin" and e.type == "branch"
    ]
    assert names == ["branch-0", "branch-1", "branch-2"]


@pytest.mark.xfail(
    strict=True,
    reason="Finding (b): replay inside the mock still emits ModelEvents; "
    "seam S3 must serve recorded outputs above the provider.",
)
def test_replayed_calls_emit_no_model_events(spike):
    log, r = spike
    model_events = [e for e in log.samples[0].events if e.event == "model"]
    live = len(r["r0"].calls) + r["r1"].live_calls
    assert len(model_events) == live


def test_random_ids_break_replay_without_id_seam(offline_module, scratch_dir):
    """Finding (a): Orbit puts random IDs into model-visible text, so replay needs S0."""
    _, r = run_spike(str(scratch_dir / "logs-spike-random"), deterministic_ids=False)
    assert "branch1_desync" in r
