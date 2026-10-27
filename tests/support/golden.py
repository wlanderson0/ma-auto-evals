"""Mock-run rows that the measurements refactor must not change (issue #19, stage 1).

Run ``uv run python -m tests.support.golden`` on a known-good tree to record them.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from inspect_ai.model import ChatMessageAssistant, ModelOutput, get_model
from inspect_ai.tool import ToolCall

from experiments.run import _mock_models, run_condition
from experiments.scenarios import SCENARIOS

GOLDEN_DIR = Path(__file__).parents[1] / "e2e" / "golden"
MEASURED = ("agents", "branch_results", "verdict", "reviewer_said", "seat_posts", "seat_mentions")
CASES: list[tuple[str, str]] = [
    (name, condition) for name in sorted(SCENARIOS) for condition in ("baseline", "l1", "l0")
] + [("kestrel_naming", "l1-rollback")]


def rollback_auditor(messages, tools, tool_choice, config) -> ModelOutput:
    """An L1 auditor that plants one post, rolls back once at the end, then ends."""
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


def case_rows(tmp_path: Path, name: str, condition: str) -> dict[str, Any]:
    models = _mock_models()
    models["probe"] = models["judge"]
    if condition == "l1-rollback":
        models["auditor"] = get_model("mockllm/auditor", custom_outputs=rollback_auditor,
                                      memoize=False)
        condition = "l1"
    [row] = run_condition(SCENARIOS[name]({}), condition, 1, tmp_path, models)
    return json.loads(json.dumps({k: row[k] for k in MEASURED if k in row},
                                 default=str, sort_keys=True))


def path_for(name: str, condition: str) -> Path:
    return GOLDEN_DIR / f"{name}--{condition}.json"


def record() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name, condition in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            rows = case_rows(Path(tmp), name, condition)
        path_for(name, condition).write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n")
        print("recorded", path_for(name, condition).name)


if __name__ == "__main__":
    record()
