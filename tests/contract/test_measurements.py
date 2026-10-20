"""The runner's per-branch record, through the real Orbit runtime (issue #19, stage 1).

The release scenario's schedule is pm, coder, coder, pm, coder, reviewer. In the
rollback test the policy forks at PM's second generation and replaces it; in the
no-op test it forks at the reviewer's generation, so pm and coder are replayed in full.
"""

from __future__ import annotations

from collections import Counter

from orbit_audit.boundaries import Boundary
from orbit_audit.branches import AuditLimits
from orbit_audit.measurements import (
    MEASUREMENTS_KEY,
    MEASUREMENTS_VERSION,
    target_model_calls,
)
from tests.contract.test_rollback import PURE, RollbackBeforeVerdict
from tests.support.runs import run_branches, sample
from tests.support.scripted import normalize_input


def reviewer_generation(view):
    return (view.boundary, view.agent) == (Boundary.GENERATION, "reviewer")


def test_every_branch_records_authors_generations_and_the_request_each_agent_saw(tmp_path):
    policy = RollbackBeforeVerdict()
    log, controllers = run_branches(tmp_path, lambda: policy, tool_replay=PURE)
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


def test_an_agent_replayed_in_full_points_at_the_parent_event(tmp_path):
    policy = RollbackBeforeVerdict(fork_at=reviewer_generation, interventions=())
    log, controllers = run_branches(tmp_path, lambda: policy, tool_replay=PURE)
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
