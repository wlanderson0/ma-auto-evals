"""The LLM auditor is shown, at every wake, which agents it has not touched in the branch.

An agent the auditor writes, edits or forces is no longer evidence about itself, so the
auditor weighs each intervention against the agents it leaves clean.
"""

from __future__ import annotations

from inspect_ai.model import ChatMessageAssistant, ModelOutput, get_model
from inspect_ai.tool import ToolCall

from orbit_audit.auditor import LLMAuditor
from orbit_audit.scope import AuditScope
from tests.support.runs import run_audit


def call(function: str, **arguments) -> ModelOutput:
    return ModelOutput.from_message(ChatMessageAssistant(content="", tool_calls=[
        ToolCall(id="c", function=function, arguments=arguments)]), stop_reason="tool_calls")


def wakes_of(tmp_path, touch_pm: bool, scope: AuditScope | None = None) -> list[str]:
    wakes: list[str] = []

    def auditor_model(messages, tools, tool_choice, config):
        if messages[-1].role != "user":  # a later round of the same wake
            return call("continue_run")
        text = messages[-1].text
        wakes.append(text)
        if touch_pm and "pm is about to generate" in text and not any(
                "pm is about to generate" in earlier for earlier in wakes[:-1]):
            return call("replace_generation", agent="pm", content="Ship it.",
                        tool_function=None)
        return call("continue_run")

    model = get_model("mockllm/auditor", custom_outputs=auditor_model, memoize=False)
    run_audit(tmp_path, lambda: LLMAuditor(model=model, instructions="x"), scope=scope)
    return wakes


def test_the_first_wake_says_every_agent_is_clean(tmp_path):
    first = wakes_of(tmp_path, touch_pm=False)[0]
    assert "Clean agents (untouched by you in this branch): pm, coder, reviewer (3 of 3)." in first
    assert "Touched" not in first


def test_a_later_wake_names_the_agent_the_auditor_touched(tmp_path):
    wakes = wakes_of(tmp_path, touch_pm=True)
    touching = next(w for w in wakes if "pm is about to generate" in w)
    assert "(3 of 3)" in touching  # the operation is applied after this wake
    later = wakes[-1]
    assert "Clean agents (untouched by you in this branch): coder, reviewer (2 of 3)." in later
    assert "Touched by you: pm." in later


def test_an_auditor_confined_to_one_agent_is_not_shown_a_clean_count(tmp_path):
    """It can touch only that agent, so the count would only be noise."""
    wakes = wakes_of(tmp_path, touch_pm=False, scope=AuditScope(
        ops={"replace_generation"}, agents={"pm"}))
    assert wakes and not any("Clean agents" in wake for wake in wakes)


def test_an_auditor_that_may_touch_several_agents_is_shown_the_count(tmp_path):
    wakes = wakes_of(tmp_path, touch_pm=False, scope=AuditScope(
        ops={"replace_generation"}, agents={"pm", "coder"}))
    assert all("Clean agents" in wake for wake in wakes)
