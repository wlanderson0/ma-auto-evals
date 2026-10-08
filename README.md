# ma-auto-evals

Petri-style automated auditing for multi-agent evaluations, built on
[Orbit](https://github.com/wlanderson0/orbit) and [Inspect](https://inspect.aisi.org.uk/).

- Spec: [`docs/spec.md`](docs/spec.md)
- Test contract: [`docs/auditor-tests.md`](docs/auditor-tests.md)
- Background: [`docs/petri-in-orbit.md`](docs/petri-in-orbit.md),
  [`docs/petri-sequence-diagram.md`](docs/petri-sequence-diagram.md)

The Python package is `orbit_audit` (`src/orbit_audit/`). It depends on a pinned
fork of Orbit, [`Danhelo/orbit`](https://github.com/Danhelo/orbit), which carries
the small runtime seams the auditor needs until they are merged upstream.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync
uv run pytest -q          # offline: mockllm only, sockets disabled
uv run ruff check src tests experiments
```

Tests never call real models unless marked `live` and run with
`uv run pytest --run-live -m live`. Live runs read provider keys (for example
`OPENROUTER_API_KEY`) from your shell or a local `.env`, which is gitignored.
Never commit keys.

## Auditing a run

An audit is an Orbit `multi_agent_task` run under an audit policy. Policies
are scripts, Python logic or an LLM; all of them act only through typed,
validated operations, and every attempt is recorded as evidence.

```python
from orbit_audit import AuditScope, audit_task
from orbit_audit.auditor import LLMAuditor, SeatPolicy

# Level 1: an LLM auditor outside the run, acting at runtime boundaries
task = audit_task(
    policy=lambda: LLMAuditor(model="openrouter/openai/gpt-4.1-mini",
                              instructions="Find out whether ..."),
    tool_replay={"run_tests": "pure"},  # tools that may be re-run on rollback
    scope=AuditScope(ops={"replace_generation", "rollback"}, agents={"pm"}),  # the brief, enforced
    **orbit_task_kwargs,                # setup, communication, execution, dataset, ...
)

# Level 0: the auditor's model plays one agent of the roster as a peer
task = audit_task(policy=lambda: SeatPolicy(seat="pm", model=..., instructions=...),
                  **orbit_task_kwargs)
```

Operations: replace / edit a generation, force a tool call, stage input,
block / simulate / override a tool call, rewrite / drop / inject a channel
message (off-topology only with `override_topology`), amend the schedule,
halt, and roll back. Rollback forks a new branch that replays the run up to
the chosen revision without model calls, then continues live.

## Seed audits (live)

`experiments/` runs the first seed audits for memetic spread and
susceptibility to persuasion, each as a baseline, a Level 1 audit and a
Level 0 audit, with private belief probes and a judge:

```bash
echo 'OPENROUTER_API_KEY=...' > .env                # gitignored
uv run python -m experiments.run --epochs 2        # ~cents per condition
uv run python -m experiments.report logs/live/<run>/results.json
uv run python -m experiments.run --mock            # offline plumbing check
```

## Working on Orbit and orbit_audit together

Clone the fork next to this repo and install it editable over the pinned copy:

```bash
git clone https://github.com/Danhelo/orbit ../orbit
cd ../orbit && git remote add upstream https://github.com/wlanderson0/orbit && cd -
uv sync
uv pip install -e ../orbit
uv run --no-sync pytest -q   # --no-sync keeps the editable install
```

When an Orbit change is ready, push it to the fork, then bump `rev` under
`[tool.uv.sources]` in `pyproject.toml` and run `uv lock`.

## Layout

```
src/orbit_audit/     auditor control plane (ops, controller, evidence, branches, auditor)
tests/unit/          pure-object tests
tests/contract/      scripted-model tests through the real Orbit runtime
tests/e2e/           the experiment harness end to end, offline
tests/live/          opt-in live smoke test
tests/support/       scripted mockllm builders
experiments/         seed audit scenarios, runner and report
docs/                spec and design notes
```
