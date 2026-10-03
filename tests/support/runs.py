"""Run scripted release-team audits and read their logs."""

from __future__ import annotations

from pathlib import Path

from inspect_ai import eval as inspect_eval
from inspect_ai.log import EvalLog, EvalSample, resolve_sample_attachments
from inspect_ai.model import get_model
from orbit import multi_agent_task
from orbit.ids import id_scope, sequential_ids

from orbit_audit.controller import AuditController
from orbit_audit.task import audit_task
from tests.support.scenarios import AGENTS, release_models, release_team
from tests.support.scripted import agent_of, normalize_input


def run_audit(tmp_path: Path, policy, **overrides) -> tuple[EvalLog, AuditController]:
    log, controllers = run_branches(tmp_path, policy, **overrides)
    return log, controllers[0]


def run_branches(tmp_path: Path, policy, *, models=release_models,
                 **overrides) -> tuple[EvalLog, list[AuditController]]:
    """Run an audit; return the log and every branch's controller, in order."""
    controllers: list[AuditController] = []
    task = audit_task(policy=policy, on_controller=controllers.append, **release_team(**overrides))
    model = get_model("mockllm/model", custom_outputs=models, memoize=False)
    with id_scope(sequential_ids()):
        log = inspect_eval(task, model=model, log_dir=str(tmp_path), display="none")[0]
    assert log.status == "success", log.samples[0].error
    return log, controllers


def run_plain(tmp_path: Path) -> EvalLog:
    model = get_model("mockllm/model", custom_outputs=release_models, memoize=False)
    with id_scope(sequential_ids()):
        log = inspect_eval(multi_agent_task(**release_team()), model=model,
                           log_dir=str(tmp_path), display="none")[0]
    assert log.status == "success"
    return log


def sample(log: EvalLog) -> EvalSample:
    """The single sample, with Inspect's deduplicated attachments resolved."""
    return resolve_sample_attachments(log.samples[0])


def model_events(log: EvalLog, model: str = "mockllm/model"):
    """Target-model events (auditor models use other mockllm names)."""
    return [e for e in sample(log).events if e.event == "model" and e.model == model]


def model_inputs(log: EvalLog) -> list[tuple[str, list]]:
    return [(agent_of(e.input, AGENTS), normalize_input(e.input)) for e in model_events(log)]


def verdict(log: EvalLog) -> str:
    return log.samples[0].output.completion


def info(log: EvalLog, name: str) -> list[dict]:
    return [e.data for e in sample(log).events
            if e.event == "info" and isinstance(e.data, dict) and e.data.get("event") == name]
