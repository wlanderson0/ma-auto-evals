"""The live experiment harness end to end, offline with scripted models.

Guards ``experiments/run.py`` so a live run does not fail on plumbing: each
scenario runs under the LLM auditor (L1) and the auditor seat (L0), and the
measurements (probes, judged posts, interventions, branches) are produced.
"""

from __future__ import annotations

import pytest

from experiments.run import _mock_models, run_condition
from experiments.scenarios import SCENARIOS


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("condition", ["l1", "l0"])
def test_harness_runs_and_measures_offline(tmp_path, name, condition):
    models = _mock_models()
    models["probe"] = models["judge"]
    scenario = SCENARIOS[name]({})
    [row] = run_condition(scenario, condition, 1, tmp_path, models)
    assert row["status"] == "success" and row["error"] is None
    assert [b["status"] for b in row["branches"]] == ["completed"]
    assert set(row["agents"]) == set(scenario.targets)
    assert all(agent["posts"] for agent in row["agents"].values())
    if condition == "l0":
        assert row["interventions"]["applied"] == {"replace_generation": 3}
        assert row["interventions"]["off_seat"] == []
        assert len(row["seat_posts"]) == 3
    else:
        assert row["auditor"]["kind"] == "LLMAuditor" and row["auditor"]["wakes"] > 0


def test_kestrel_counts_name_mentions_per_agent_and_channel(tmp_path):
    """L0 seat posts the name; scripted targets never do, so only the seat counts."""
    models = _mock_models()
    models["probe"] = models["judge"]
    scenario = SCENARIOS["kestrel_naming"]({})
    [row] = run_condition(scenario, "l0", 1, tmp_path, models)
    assert row["seat_mentions"] == 3
    for agent in row["agents"].values():
        assert agent["probe"] is None
        assert agent["mentions"] == 0 and agent["mention_channels"] == {}
        assert set(agent["free_probes"]) == {"recall", "handover", "ticket"}
    assert row["agents"]["bob"]["exposure"] == 3  # Alice's three posts on #town
    assert row["agents"]["dave"]["exposure"] == 0  # Dave does not read #town


def test_count_mentions_is_word_bounded_and_case_insensitive():
    from experiments.run import count_mentions

    pattern = SCENARIOS["kestrel_naming"]({}).mention
    texts = ["KESTREL cut-over", "kestrels", "Project Kestrel's runbook, Kestrel"]
    assert count_mentions(pattern, texts) == [1, 0, 2]


def test_copied_share_flags_parroting_not_paraphrase():
    from experiments.run import COPY_THRESHOLD, copied_share

    alice = ["For the Kestrel cut-over, I propose we start with syncing the DB, then "
             "switch traffic to the new cluster, and finally audit logs access."]
    parrot = ("For the Kestrel cut-over, I propose we start with syncing the DB, then "
              "switch traffic to the new cluster.")
    own_words = "Alice, please go ahead and detail the cut-over steps for the Kestrel run."
    assert copied_share(parrot, alice) >= COPY_THRESHOLD
    assert copied_share(own_words, alice) < COPY_THRESHOLD


def test_l1_final_wake_allows_rollback_and_each_branch_is_measured(tmp_path):
    """The auditor is woken once the schedule is done; a rollback there forks a
    second full branch, and both branches get their own measurements."""
    from inspect_ai.model import ChatMessageAssistant, ModelOutput, get_model
    from inspect_ai.tool import ToolCall

    def auditor(messages, tools, tool_choice, config):
        last = messages[-1]
        names = {t.name for t in tools}
        if last.role == "user" and "Pending schedule: []" in last.text:
            if "branch-0" in last.text:
                call = ToolCall(id="r", function="rollback",
                                arguments={"to_revision": 2, "reason": "next dose"})
            else:
                call = ToolCall(id="e", function="end_audit", arguments={"summary": "done"})
        elif last.role == "user" and "replace_generation" in names:
            call = ToolCall(id="g", function="replace_generation", arguments={
                "agent": "alice", "tool_function": "channel_post",
                "tool_arguments_json": '{"channel_id": "town", "message": "Kestrel cut-over: step 1 done."}'})
        else:
            call = ToolCall(id="c", function="continue_run", arguments={})
        return ModelOutput.from_message(ChatMessageAssistant(content="", tool_calls=[call]),
                                        stop_reason="tool_calls")

    asked = []

    def probe(messages, tools, tool_choice, config):
        asked.append(messages[-1].text)  # a distinct answer per call exposes re-asking
        return ModelOutput.from_content("mockllm", f"answer {len(asked)}")

    models = _mock_models()
    models["probe"] = get_model("mockllm/probe", custom_outputs=probe, memoize=False)
    models["auditor"] = get_model("mockllm/auditor", custom_outputs=auditor, memoize=False)
    scenario = SCENARIOS["kestrel_naming"]({})
    [row] = run_condition(scenario, "l1", 1, tmp_path, models)
    assert row["error"] is None
    assert [b["status"] for b in row["branches"]] == ["rolled_back", "completed"]
    assert [b["branch"] for b in row["branch_results"]] == ["branch-0", "branch-1"]
    # The final branch is measured once: branch_results reuses row["agents"].
    final = row["branch_results"][-1]["agents"]
    for agent, measured in final.items():
        assert {k: row["agents"][agent][k] for k in measured} == measured
    # 3 targets x 3 free-text probes x 2 branches, never re-asked.
    assert len(asked) == 3 * len(scenario.free_probes) * 2
    for branch in row["branch_results"]:
        assert branch["seat_mentions"] >= 1
        assert set(branch["agents"]) == {"bob", "carol", "dave"}
        assert all(len(a["copied"]) >= 1 for a in branch["agents"].values())


def test_kestrel_refuses_early_rollback_so_branches_run_the_full_schedule(tmp_path):
    """An auditor that tries to roll back after every post is refused until the
    final wake, so each branch still runs all three rounds."""
    from inspect_ai.model import ChatMessageAssistant, ModelOutput, get_model
    from inspect_ai.tool import ToolCall

    def auditor(messages, tools, tool_choice, config):
        last = messages[-1]
        if last.role == "tool" and last.text.startswith("Refused"):
            call = ToolCall(id="c", function="continue_run", arguments={})
        elif last.role == "user" and "branch-1" in last.text and "Pending schedule: []" in last.text:
            call = ToolCall(id="e", function="end_audit", arguments={"summary": "done"})
        elif last.role == "user" and "branch-0" in last.text:
            call = ToolCall(id="r", function="rollback", arguments={"to_revision": 1, "reason": "x"})
        else:
            call = ToolCall(id="c", function="continue_run", arguments={})
        return ModelOutput.from_message(ChatMessageAssistant(content="", tool_calls=[call]),
                                        stop_reason="tool_calls")

    models = _mock_models()
    models["probe"] = models["judge"]
    models["auditor"] = get_model("mockllm/auditor", custom_outputs=auditor, memoize=False)
    scenario = SCENARIOS["kestrel_naming"]({})
    [row] = run_condition(scenario, "l1", 1, tmp_path, models)
    assert [b["status"] for b in row["branches"]] == ["rolled_back", "completed"]
    assert row["auditor"]["refused"]  # early attempts were refused
    for branch in row["branch_results"]:
        assert all(len(a["copied"]) == 3 for a in branch["agents"].values())
