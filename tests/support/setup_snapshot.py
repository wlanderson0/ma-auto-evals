"""Built scenario config that parameterising the setup must not change (L2 Task 1).

Run ``uv run python -m tests.support.setup_snapshot`` on a known-good tree to record it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from inspect_ai.tool import ToolDef

from experiments.run import TARGETS
from experiments.scenarios import SCENARIOS

SNAPSHOT = Path(__file__).parents[1] / "unit" / "golden" / "scenario_setup.json"


class _Context:
    agent_name = "carol"


def _tool(factory: Any) -> dict[str, Any]:
    """What a binding's factory makes for one agent (factories are closures)."""
    made = ToolDef(factory.create(_Context()))
    return {"name": made.name, "description": made.description,
            "parameters": made.parameters.model_dump(mode="json")}


def built(name: str) -> dict[str, Any]:
    """Every part of a scenario's ``task`` kwargs that reaches Orbit, as JSON."""
    return dump_task(SCENARIOS[name](TARGETS[name]).task)


def dump_task(task: dict[str, Any]) -> dict[str, Any]:
    """``task`` kwargs as JSON."""
    dumped: dict[str, Any] = {}
    for key, value in sorted(task.items()):
        if key == "tool_bindings":
            dumped[key] = {binding: _tool(factory) for binding, factory in sorted(value.items())}
        elif key == "dataset":
            dumped[key] = [sample.model_dump(mode="json") for sample in value]
        elif hasattr(value, "model_dump"):
            dumped[key] = value.model_dump(mode="json")
        else:
            dumped[key] = value
    return json.loads(json.dumps(dumped, sort_keys=True))


def record() -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps({name: built(name) for name in sorted(SCENARIOS)},
                                   indent=1, sort_keys=True) + "\n")
    print("recorded", SNAPSHOT)


if __name__ == "__main__":
    record()
