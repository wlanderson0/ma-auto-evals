"""No-op rollback is the identity (``docs/auditor-tests.md`` R7).

R1-R3 compare a child's replayed prefix with its parent, within one run. R7
checks the whole timeline end to end: for every fork point k, rolling back to
k with no interventions and letting the child continue live must reproduce a
separate plain (no-rollback) run -- same boundary sequence, same model inputs
and outputs, same verdict, and pure tools executed once in the child's store
(no double side effects). With deterministic scripted models any difference is
a replay bug, not model noise.

Step fingerprints are compared only within one run (child prefix vs parent),
not against the separate reference run: they hash raw ids that legitimately
differ across runs (mockllm tool-call UUIDs, and Orbit ids minted after the
fork, which are unique to each branch), so the cross-run checks compare
id-renumbered content instead. Those ids reach the models, so they must also
carry no visible structure that would reveal the branch.
"""

from __future__ import annotations

import json
import re

import pytest

from orbit_audit.boundaries import Boundary
from orbit_audit.ops import Rollback
from orbit_audit.policy import AuditPolicy
from orbit_audit.runner import BRANCHES_KEY
from tests.support.runs import info, run_branches, sample, verdict

PURE = {"run_tests": "pure"}


class Observe(AuditPolicy):
    """Never intervenes: the reference timeline."""

    name = "observe"

    async def decide(self, view):
        return []


class NoopRollback(AuditPolicy):
    """At Reviewer's generation in the baseline, roll back to ``k`` and change nothing."""

    name = "noop-rollback"

    def __init__(self, k: int):
        self.k = k

    async def decide(self, view):
        if view.branch == "branch-0" and (view.boundary, view.agent) == (
                Boundary.GENERATION, "reviewer"):
            return [Rollback(to_revision=self.k)]
        return []


# mockllm tool-call UUIDs, and Orbit ids, which differ per branch after a fork.
# Both are renumbered.
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                  r"|\b[a-z_]+-[0-9a-f]{32}\b")


def renumber(text: str) -> str:
    """Replace ids by their order of first use.

    Two separate runs never share mockllm's tool-call ids, and a child branch's
    Orbit ids differ from its parent's after the fork, by design.
    """
    seen: dict[str, str] = {}
    return UUID.sub(lambda m: seen.setdefault(m.group(), f"id{len(seen)}"), text)


# An Orbit id with a run of 8+ zeros has visible structure: sequential ids
# (message-000...0001) or the branch index packed into the high bits
# (message-0000000000000001...). For a hashed id the chance is ~1e-8.
STRUCTURED_ID = re.compile(r"\b[a-z_]+-(?=[0-9a-f]{32}\b)[0-9a-f]*0{8}[0-9a-f]*")


def structured_ids(controller) -> set[str]:
    """Orbit ids with visible structure anywhere in the models' inputs."""
    return set(STRUCTURED_ID.findall(json.dumps(
        [[m.model_dump(mode="json", exclude={"id"}) for m in g.input]
         for g in controller.generations], default=str)))


def strip_ids(controller) -> str:
    return renumber(json.dumps(
        [(g.agent, [m.model_dump(mode="json", exclude={"id"}) for m in g.input])
         for g in controller.generations], sort_keys=True, default=str))


def timeline(controller) -> str:
    """Boundary sequence plus what each model produced (text and tool calls)."""
    return renumber(json.dumps(
        [(s.label, s.agent, s.turn, s.executed,
          [(o.completion, [(c.function, c.arguments) for c in (o.message.tool_calls or [])])
           for o in s.outputs])
         for s in controller.steps], sort_keys=True, default=str))


@pytest.fixture(scope="module")
def reference(offline_module, scratch_dir):
    log, (ctl,) = run_branches(scratch_dir / "noop-ref", Observe, tool_replay=PURE)
    fork_limit = next(s.revision for s in ctl.steps
                      if (s.boundary, s.agent) == (Boundary.GENERATION, "reviewer"))
    return log, ctl, fork_limit


@pytest.mark.slow
def test_r7_noop_rollback_reproduces_the_plain_run_at_every_fork_point(reference, tmp_path):
    ref_log, ref, fork_limit = reference
    assert fork_limit > 1, "scenario too short to fork"
    problems: list[str] = []
    for k in range(1, fork_limit + 1):
        log, controllers = run_branches(tmp_path / f"k{k}", lambda k=k: NoopRollback(k),
                                        tool_replay=PURE)
        tree = sample(log).store[BRANCHES_KEY]
        if [b["status"] for b in tree] != ["rolled_back", "completed"]:
            problems.append(f"k={k}: statuses {[b['status'] for b in tree]} "
                            f"{[b.get('reason') for b in tree]}")
            continue
        parent, child = controllers[0], controllers[-1]
        # Within one run ids are consistent, so the replayed prefix must match
        # the parent's fingerprints exactly (R1-R2, at every k).
        if [x.fingerprint for x in child.steps[:k - 1]] != [
                x.fingerprint for x in parent.steps[:k - 1]]:
            problems.append(f"k={k}: replayed prefix fingerprints differ from parent")
        if timeline(child) != timeline(ref):
            problems.append(f"k={k}: timeline differs "
                            f"({len(child.steps)} vs {len(ref.steps)} steps)")
        if strip_ids(child) != strip_ids(ref):
            problems.append(f"k={k}: model inputs differ")
        if leaked := structured_ids(child):
            problems.append(f"k={k}: model-visible ids reveal the branch: {sorted(leaked)[:3]}")
        if verdict(log) != verdict(ref_log):
            problems.append(f"k={k}: verdict {verdict(log)!r} != {verdict(ref_log)!r}")
        runs, ref_runs = (sample(x).store.get("RepoEnv:test_runs") for x in (log, ref_log))
        if runs != ref_runs:
            problems.append(f"k={k}: run_tests executed {runs}x in final store, "
                            f"reference {ref_runs}x (double side effect?)")
        # Everything before the fork is replayed, never re-sent to the provider.
        replayed = sum(e.get("replayed_calls", 0) or 0
                       for e in info(log, "orbit_generation_control"))
        expected = sum(1 for s in ref.steps
                       if s.boundary == Boundary.GENERATION and s.revision < k)
        if replayed != expected:
            problems.append(f"k={k}: {replayed} replayed generations, expected {expected}")
    assert not problems, "\n".join(problems)
