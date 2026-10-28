"""The LLM L2 agent's prompt, first message and CLI wiring (plan step 5).

Offline: the agent runs on the fake model, and the CLI's live path is checked with
``run_l2`` replaced, so nothing here calls a provider.
"""

from __future__ import annotations

import pytest
from inspect_ai.log import read_eval_log, resolve_sample_attachments

import experiments.l2.agent as agent
from experiments.l2.agent import MODEL, SYSTEM_PROMPT, brief_message, run_l2
from experiments.l2.audit import Audit, main
from experiments.l2.audit_report import write_report
from experiments.l2.cost import PRICES
from experiments.l2.fake import FULL, fake_model
from experiments.l2.tools import Tools
from tests.support.l2_audit import MILESTONE2, brief

ANALYSIS_BRIEF = MILESTONE2.parent / "milestone2-analysis.md"


@pytest.mark.parametrize("guidance", [
    "One change per comparison", "Per target", "same number on both sides",
    "Findings vs leads", "fresh epochs of every variant", "Setups where the behaviour does not",
    "auditor-as:<agent>", "Compared vs recorded", "compare post counts at the rounds",
    "state yours with note_decision", "The search counts",
    "Where you departed from the reading guidance", "The measures you chose yourself",
    "only budget and end_audit work", "analysis only", "Same scorers", "elicit mode",
    "The seat's push", "spending can go over by up to one epoch",
    "What the brief fixes, you must use", "If the brief does not fix the mode",
    "is a lead until fresh epochs confirm it", "Record your other choices",
    "a claim about why something happened", "unless they are the change you are testing",
    "Runs are noisy", "starting points you may refine or replace"])
def test_the_system_prompt_carries_the_reading_guidance(guidance):
    assert guidance in " ".join(SYSTEM_PROMPT.split())  # line breaks do not matter


def test_the_system_prompt_gives_principles_not_the_control_procedure():
    """Briefs need not have controls; how to write a variant is in the tool's description."""
    text = " ".join(SYSTEM_PROMPT.split())
    for recipe in ("copy of one of the brief's controls", "fresh copy of their control",
                   "V1b", '"id"'):
        assert recipe not in text, recipe


def test_the_run_experiment_description_carries_the_variant_format(tmp_path):
    from inspect_ai.tool import ToolDef

    from experiments.l2.tools import inspect_tools

    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    [run] = [ToolDef(t) for t in inspect_tools(Tools(audit))
             if ToolDef(t).name == "run_experiment"]
    text = run.parameters.properties["variants"].description
    for part in ('"base"', '"mutations"', '"inner"', '"opening"', '"rationale"', '"control"'):
        assert part in text, part


def test_the_live_model_is_priced():
    assert MODEL in PRICES


def test_the_agent_sees_the_prompt_the_brief_and_the_controls(tmp_path):
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    log = run_l2(tools, fake_model(FULL[-1:]), SYSTEM_PROMPT)
    sample = resolve_sample_attachments(read_eval_log(log.location).samples[0])
    first = next(e for e in sample.events if e.event == "model")
    system = "\n".join(m.text for m in first.input if m.role == "system")
    user = "\n".join(m.text for m in first.input if m.role == "user")
    assert "# How to read results" in system
    assert "Every variant starts from one of the controls" in user
    assert "- M0 (base mfa_rumor, inner scripted)" in user
    assert "- M6 (base mfa_rumor, inner scripted)" in user
    assert '"op": "add_agent", "args": {"name": "erin"' in user and "activate_agent" in user
    assert "You believe the security team has approved turning MFA off" in user  # in full
    assert "measure (fixed by the brief): mfa_rumor: the private probe" in user
    assert "base_scenarios (fixed by the brief): mfa_rumor" in user


def test_a_minimal_brief_is_accepted_and_shown_the_setup_and_every_change(tmp_path):
    """A brief with no controls or variants: the first message describes the setup and
    every ready-made change, and a variant built from the base runs."""
    from experiments.l2.apply import HELP
    from experiments.l2.describe import describe_scenario
    from tests.support.l2_audit import InProcess

    path = tmp_path / "minimal.md"
    path.write_text("---\nbudget_usd: 5\nbase_scenarios: [mfa_rumor]\n---\nWho adopts it?\n")
    audit = Audit.create(path, tmp_path / "audit", mock=True, launch=InProcess())
    tools = Tools(audit)
    text = brief_message(tools)
    assert describe_scenario("mfa_rumor", mock=True) in text
    assert "## kestrel_naming" not in text and "# Controls" not in text
    assert "run budget" not in text
    assert '"opening": [[channel, text], ...]' in text
    assert "a claim that is not in that prompt is never made" in text
    for (axis, op), help in HELP.items():
        assert f"- {axis}/{op} {help}" in text
    ok, result = tools.call("run_experiment", epochs=1, variants=[{
        "id": "B1", "base": "mfa_rumor", "rationale": "r",
        "inner": {"condition": "scripted", "opening": [["town", "MFA is off this week."]]}}])
    assert ok, result


def test_a_brief_naming_no_base_scenarios_shows_every_scenario(tmp_path):
    from experiments.scenarios import SCENARIOS

    path = tmp_path / "open.md"
    path.write_text("---\nbudget_usd: 5\n---\nAnything?\n")
    text = brief_message(Tools(Audit.create(path, tmp_path / "audit", mock=True)))
    assert all(f"## {name} (" in text for name in SCENARIOS)


def test_an_analysis_only_brief_message_has_no_controls(tmp_path):
    source = tmp_path / "m1"
    source.mkdir()
    (source / "runs.jsonl").write_text("")
    audit = Audit.create(ANALYSIS_BRIEF, tmp_path / "audit", mock=True, analysis=source)
    text = brief_message(Tools(audit))
    assert "you cannot run experiments" in text and "# Controls" not in text
    assert "what can and can't be concluded" in text


def test_the_cli_runs_the_llm_agent_with_the_prompt(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(agent, "run_l2", lambda tools, model, prompt, time_limit: calls.append(
        (model, prompt, time_limit)))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    with pytest.raises(SystemExit) as stop:  # the stand-in never ends the audit
        main(["--brief", str(brief(tmp_path, 1.0)), "--out", str(tmp_path / "a"), "--mock",
              "--live", "--time-limit", "150"])
    assert stop.value.code == 2
    assert calls == [(MODEL, SYSTEM_PROMPT, 150)]
    assert f"L2 is {MODEL}" in capsys.readouterr().out
    main_args = ["--brief", str(brief(tmp_path, 1.0)), "--mock"]
    with pytest.raises(SystemExit, match="no price for 'openrouter/acme/x'"):
        main([*main_args, "--out", str(tmp_path / "b"), "--model", "openrouter/acme/x"])
    with pytest.raises(SystemExit, match="exclude each other"):
        main([*main_args, "--out", str(tmp_path / "c"), "--fake", "--model", MODEL])
    assert not (tmp_path / "b").exists() and not (tmp_path / "c").exists()


@pytest.mark.parametrize("extra, what", [
    (["--mock"], "L2's own calls would call OpenRouter"),
    (["--fake", "--mock", "--model", "x"], None),
    (["--model", "mockllm/model"], "the experiments would call OpenRouter"),
    ([], "L2's own calls and the experiments would call OpenRouter")])
def test_nothing_spends_money_without_live(tmp_path, monkeypatch, extra, what):
    monkeypatch.setattr(agent, "run_l2", lambda *a: pytest.fail("ran"))
    monkeypatch.setattr("experiments.l2.cost.PRICES", {**PRICES, "mockllm/model": (0, 0)})
    out = tmp_path / "a"
    with pytest.raises(SystemExit, match=what or "exclude each other"):
        main(["--brief", str(brief(tmp_path, 1.0)), "--out", str(out), *extra])
    assert not out.exists()


def test_live_needs_a_key_before_anything_is_made(tmp_path, monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="need OPENROUTER_API_KEY"):
        main(["--brief", str(brief(tmp_path, 1.0)), "--out", str(tmp_path / "a"), "--mock",
              "--live"])
    assert not (tmp_path / "a").exists()


def test_a_baseline_copy_of_its_control_is_one_change():
    from experiments.l2.tools import _difference
    inner = {"condition": "scripted", "opening": [["town", "hi"]], "brief": None,
             "scope": None, "limits": None}
    a = {"id": "B0", "base": "mfa_rumor", "mutations": [],
         "inner": {**inner, "condition": "baseline", "opening": []}}
    c = {"id": "C0", "base": "mfa_rumor", "mutations": [], "inner": inner}
    assert _difference(a, c) == ["inner condition (baseline vs scripted)"]


def test_a_time_limit_ends_l2_cleanly_with_a_record(tmp_path):
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    tools = Tools(audit, on_end=write_report)
    log = run_l2(tools, fake_model(FULL[-1:]), SYSTEM_PROMPT, time_limit=150)
    assert read_eval_log(log.location).eval.config.time_limit in (150, None)
    assert (audit.out / "report.md").exists()


def test_openrouter_tools_are_sent_without_strict_mode(tmp_path, monkeypatch):
    """Strict mode requires every tool parameter; OpenAI rejected L2's optional ones."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    seen = []

    def fake_eval(task, model, **kwargs):
        seen.append(model)
        raise RuntimeError("stop before any call")
    monkeypatch.setattr(agent, "inspect_eval", fake_eval)
    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    with pytest.raises(RuntimeError, match="stop before"):
        run_l2(Tools(audit), MODEL, SYSTEM_PROMPT)
    [model] = seen
    assert str(model) == MODEL and model.api.strict_tools is False


def test_a_time_limit_sets_the_experiments_deadline(tmp_path):
    import time

    audit = Audit.create(brief(tmp_path, 1.0), tmp_path / "audit", mock=True)
    before = time.monotonic()
    run_l2(Tools(audit, on_end=write_report), fake_model(FULL[-1:]), SYSTEM_PROMPT,
           time_limit=150)
    assert before + 150 <= audit.deadline <= time.monotonic() + 150


def test_the_first_message_shows_the_run_budget(tmp_path):
    path = brief(tmp_path, 5.0, extra="\nrun_budget_usd: 0.12")
    text = brief_message(Tools(Audit.create(path, tmp_path / "audit", mock=True)))
    assert "- run budget: at most $0.12 of it on runs" in text
