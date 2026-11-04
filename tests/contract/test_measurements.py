"""The runner's per-branch record, through the real Orbit runtime (issue #19, stage 1).

The release scenario's schedule is pm, coder, coder, pm, coder, reviewer. In the
rollback test the policy forks at PM's second generation and replaces it; in the
no-op test it forks at the reviewer's generation, so pm and coder are replayed in full.
"""

from __future__ import annotations

from collections import Counter

import pytest
from inspect_ai import eval as inspect_eval
from inspect_ai.model import get_model
from orbit.ids import id_scope, sequential_ids

from orbit_audit.boundaries import Boundary
from orbit_audit.branches import AuditLimits
from orbit_audit.measurements import (
    MEASUREMENTS_KEY,
    MEASUREMENTS_VERSION,
    target_model_calls,
)
from orbit_audit.ops import EditGeneration, StageInput, TextReplacement
from orbit_audit.policy import AuditPolicy, ScriptedPolicy, at_generation
from orbit_audit.task import audit_task
from tests.contract.test_rollback import PURE, RollbackBeforeVerdict
from tests.support.runs import run_branches, sample
from tests.support.scenarios import release_models, release_team
from tests.support.scripted import normalize_input


def reviewer_generation(view):
    return (view.boundary, view.agent) == (Boundary.GENERATION, "reviewer")


@pytest.fixture(scope="module")
def rolled_back(offline_module, scratch_dir):
    policy = RollbackBeforeVerdict()
    return run_branches(scratch_dir / "measure-rollback", lambda: policy, tool_replay=PURE)


@pytest.fixture(scope="module")
def replayed_in_full(offline_module, scratch_dir):
    policy = RollbackBeforeVerdict(fork_at=reviewer_generation, interventions=())
    return run_branches(scratch_dir / "measure-noop", lambda: policy, tool_replay=PURE)


def kind(trace) -> str:
    return "replayed" if trace.replayed else "live" if trace.exposed else "auditor-written"


def test_the_runs_above_exercise_every_kind_of_generation(rolled_back, replayed_in_full):
    kinds = {kind(t) for _, controllers in (rolled_back, replayed_in_full)
             for c in controllers for t in c.generations}
    assert kinds == {"replayed", "live", "auditor-written"}


def test_every_branch_records_authors_generations_and_the_request_each_agent_saw(rolled_back):
    log, controllers = rolled_back
    s = sample(log)
    recorded = s.store[MEASUREMENTS_KEY]
    assert recorded["version"] == MEASUREMENTS_VERSION
    assert list(recorded["branches"]) == ["branch-0", "branch-1"]  # abandoned branch kept
    events = {e.uuid: e for e in s.events if e.event == "model"}
    calls = target_model_calls(s.events)
    for controller in controllers:
        record = recorded["branches"][controller.branch]
        assert record["authors"] == controller.authors()
        assert record["generations"] == dict(Counter(t.agent for t in controller.generations))
        for agent in record["generations"]:
            traces = [t for t in controller.generations if t.agent == agent]
            ran = [t for t in traces if t.exposed and not t.replayed]
            mine = [c for c in calls if (c.branch, c.agent) == (controller.branch, agent)]
            assert len(mine) == len(ran)  # the auditor's own calls are not the agent's
            last, request = traces[-1], record["requests"][agent]
            if last.replayed:
                continue
            if last.exposed:
                assert request["via"] == controller.branch and request["event"] == mine[-1].uuid
                assert (normalize_input(events[request["event"]].input)
                        == normalize_input(list(last.input)))  # what the agent saw
            else:
                assert request["event"] is None and request["why"]


def test_each_branch_records_whom_the_auditor_touched(rolled_back):
    log, _ = rolled_back
    branches = sample(log).store[MEASUREMENTS_KEY]["branches"]
    assert branches["branch-0"]["touched"] == {}  # the baseline run, only rolled back
    [entry] = branches["branch-1"]["touched"]["pm"]
    assert entry["op"] == "replace_generation" and not entry["replayed"]
    assert list(branches["branch-1"]["touched"]) == ["pm"]  # the other agents stay clean


def test_a_fork_inherits_the_touches_made_before_its_fork_point(tmp_path):
    def stage_for_coder(view):
        at = (view.boundary, view.agent, view.turn)
        return [StageInput(agent="coder", content="note")] if at == (
            Boundary.BEFORE_ACTIVATION, "coder", 1) else []

    policy = RollbackBeforeVerdict(extra=stage_for_coder)  # forks after turn 1
    log, _ = run_branches(tmp_path, lambda: policy, tool_replay=PURE)
    branches = sample(log).store[MEASUREMENTS_KEY]["branches"]
    assert [(e["op"], e["replayed"]) for e in branches["branch-0"]["touched"]["coder"]] == [
        ("stage_input", False)]
    assert [(e["op"], e["replayed"]) for e in branches["branch-1"]["touched"]["coder"]] == [
        ("stage_input", True)]


def test_an_agent_replayed_in_full_points_at_the_parent_event(replayed_in_full):
    log, controllers = replayed_in_full
    parent, child = controllers
    branches = sample(log).store[MEASUREMENTS_KEY]["branches"]
    for agent in ("pm", "coder"):
        assert all(t.replayed for t in child.generations if t.agent == agent)
        assert branches["branch-1"]["requests"][agent] == branches["branch-0"]["requests"][agent]
        assert branches["branch-1"]["requests"][agent]["via"] == "branch-0"
    assert branches["branch-1"]["requests"]["reviewer"]["via"] == "branch-1"


def test_the_record_is_written_only_to_the_sample_store_never_a_branch_store(tmp_path):
    policy = RollbackBeforeVerdict()
    _, controllers = run_branches(tmp_path, lambda: policy, tool_replay=PURE)
    for controller in controllers:
        assert MEASUREMENTS_KEY not in controller.runtime.state.store.keys()


def test_a_generation_stopped_by_the_budget_has_no_model_event_and_says_so(tmp_path):
    policy = RollbackBeforeVerdict()
    log, controllers = run_branches(tmp_path, lambda: policy, tool_replay=PURE,
                                    limits=AuditLimits(max_provider_calls=6))
    exhausted = controllers[1]
    assert sample(log).store["orbit_audit:branches"][1]["status"] == "budget_exhausted"
    stopped = exhausted.generations[-1]
    assert (stopped.exposed, stopped.replayed) == (False, False)  # the model was never called
    record = sample(log).store[MEASUREMENTS_KEY]["branches"]["branch-1"]
    assert record["requests"][stopped.agent]["event"] is None
    whys = [r["why"] for b in sample(log).store[MEASUREMENTS_KEY]["branches"].values()
            for r in b["requests"].values()]
    assert not any("live generation(s) but" in why for why in whys)


def test_an_edited_last_generation_is_a_normal_live_entry(tmp_path):
    edit = EditGeneration(agent="coder", replacements=(
        TextReplacement(old="still failing: 3 failed", new="3 failed, all flaky"),))
    log, [controller] = run_branches(
        tmp_path, lambda: ScriptedPolicy(at_generation("coder", 2, edit)), tool_replay=PURE)
    s = sample(log)
    [trace] = [t for t in controller.generations if (t.agent, t.index) == ("coder", 2)]
    assert [r.op for r in controller.ledger.applied()] == ["edit_generation"]
    assert (trace.exposed, trace.replayed) == (True, False)  # the model ran; the auditor edited
    request = s.store[MEASUREMENTS_KEY]["branches"]["branch-0"]["requests"]["coder"]
    assert request["event"] is not None and request["via"] == "branch-0"
    [event] = [e for e in s.events if e.event == "model" and e.uuid == request["event"]]
    assert normalize_input(event.input) == normalize_input(list(trace.input))


def test_a_branch_that_errors_still_leaves_its_record_in_the_sample_store(tmp_path):
    calls = []

    def dies_on_the_third_call(messages, tools, tool_choice, config):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("provider down")
        return release_models(messages, tools, tool_choice, config)

    task = audit_task(policy=AuditPolicy, **release_team())
    model = get_model("mockllm/model", custom_outputs=dies_on_the_third_call, memoize=False)
    with id_scope(sequential_ids()):
        log = inspect_eval(task, model=model, log_dir=str(tmp_path), display="none",
                           fail_on_error=False)[0]
    s = sample(log)
    assert s.error is not None and "provider down" in s.error.message
    assert [b["status"] for b in s.store["orbit_audit:branches"]] == ["error"]
    recorded = s.store[MEASUREMENTS_KEY]["branches"]["branch-0"]
    assert recorded["generations"]  # what the branch had done before it failed
    assert set(recorded["requests"]) <= {"pm", "coder"}
    [failed] = [r for r in recorded["requests"].values() if r["event"] is None]
    assert failed["why"] == "its model call failed (1 failed call(s) in branch-0)"
