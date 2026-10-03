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
uv run ruff check src tests
```

Tests never call real models unless marked `live` and run with
`uv run pytest --run-live -m live`. Live runs read provider keys (for example
`OPENROUTER_API_KEY`) from your shell or a local `.env`, which is gitignored.
Never commit keys.

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
tests/support/       scripted mockllm builders
docs/                spec and design notes
```
