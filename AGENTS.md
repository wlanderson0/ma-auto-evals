# Agent guide

Instructions for coding agents working in ma-auto-evals. People start with the
[README](README.md).

ma-auto-evals adds Petri-style automated auditing to multi-agent evaluations on
[Orbit](https://github.com/wlanderson0/orbit) and
[Inspect](https://inspect.aisi.org.uk/). The Python package is `orbit_audit`.
The specs are [`docs/spec.md`](docs/spec.md) and the test contract
[`docs/auditor-tests.md`](docs/auditor-tests.md); read the relevant section
before changing behaviour.

## Repo map

```
src/orbit_audit/          auditor control plane: ops, controller, evidence, branches, runner, task
src/orbit_audit/auditor/  the LLM auditor (llm.py), the Level 0 seat (seat.py), realism check
experiments/              seed audits: scenarios.py, run.py (runner), report.py, findings/
tests/unit/               pure-object tests
tests/contract/           scripted-model tests through the real Orbit runtime
tests/e2e/                the experiment harness end to end, offline
tests/live/               opt-in live smoke test
tests/support/            scripted mockllm builders
docs/                     spec, test contract, scenarios, results, design notes
```

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync
```

Orbit comes from the pinned fork [`Danhelo/orbit`](https://github.com/Danhelo/orbit)
(`[tool.uv.sources]` in `pyproject.toml`). To change Orbit at the same time,
follow "Working on Orbit and orbit_audit together" in the README: clone the
fork to `../orbit`, `uv pip install -e ../orbit`, and use `uv run --no-sync` so
the editable install is kept. To move the pin, push the Orbit change to the
fork, bump `rev` and run `uv lock`.

## Tests and lint

CI (`.github/workflows/ci.yml`) runs these on Python 3.11 and 3.13. Run the
last two before you finish:

```bash
uv sync --locked
uv run --locked ruff check src tests experiments
uv run --locked pytest -q --tb=short --durations=10
```

`pyproject.toml` sets the pytest defaults: `-m 'not live'`, strict markers and
config, `--disable-socket --allow-unix-socket` (no network) and a 120 s timeout
per test. Markers:

- `live`: real API calls; needs `--run-live -m live` and a key, and costs money.
- `slow`: offline but long; runs by default. Skip it while iterating with
  `-m 'not live and not slow'`.

Ruff: line length 100, rules E, F, I, B and UP.

## Offline by default

Everything except live runs works without a key or network. Tests generate
only with `mockllm/` models (see `tests/support/`). The runner has a scripted
mode:

```bash
uv run python -m experiments.run --mock   # no network, no cost
```

CI sets these variables; set them too when a command must not touch the network
or a local `.env`:

```bash
export PYTHON_DOTENV_DISABLED=1 ORBIT_AUTOFETCH=0 ORBIT_AUTOSTART_SERVICE=0 UV_NO_ENV_FILE=1
```

`PYTHON_DOTENV_DISABLED` stops `.env` loading through python-dotenv (Inspect
loads `.env` on import), `UV_NO_ENV_FILE` stops `uv run` loading env files,
and the `ORBIT_*` pair stops Orbit fetching BrowserART data and starting its
browser service. `tests/conftest.py` sets both `ORBIT_*` variables for every
pytest run, and `PYTHON_DOTENV_DISABLED` unless `--run-live` is given.

## Live runs

Live runs call real models through OpenRouter and cost money. Run them only
when the user asks. They read `OPENROUTER_API_KEY` from the environment or a
gitignored `.env` that the user provides; never write a key yourself.

```bash
uv run python -m experiments.run --epochs 2 --pr <N>
uv run python -m experiments.report logs/live/<run>/results.json
uv run pytest --run-live -m live
```

## Sharing a run with cosmos

Runs are shared on [cosmos](https://cosmos.danhelo.com). Share a run when the
user asks, or when the PR changes what a run records or how it shows (runner,
scenarios, `orbit_audit_*` events, authorship). Upload the run folder and put
the printed link in the PR description, with one line on what the run shows:

```bash
uv run python -m experiments.run --mock --pr <N>   # or a live run, when asked
cosmos upload logs/live/<run> --dry-run           # checks files and manifest; needs no token
cosmos upload logs/live/<run>                     # prints https://cosmos.danhelo.com/runs/<slug>
```

Run `cosmos upload` from the repo root. It needs the `cosmos` CLI, a token
(`cosmos login`, or `COSMOS_URL` and `COSMOS_TOKEN`) and network access to
cosmos. If any of these is missing, or the upload fails, say so in your report
and do not work around it. Never print, log or commit a token. If the upload is
refused by the secret scan, a key may have leaked into the logs: stop and tell
the user. Details: [`docs/cosmos.md`](docs/cosmos.md).

## The contract cosmos reads

[Danhelo/cosmos](https://github.com/Danhelo/cosmos) parses the run folders this
repo writes. Changing any of the following needs a matching change there, or
runs stop showing correctly. Start with `src/cosmos/ingest/parse.py` and
`src/cosmos/ingest/load.py`; for the manifest also `src/cosmos/cli.py` and
`src/cosmos/api/uploads.py`; and in the web app `web/src/lib/authors.ts`
(authorship labels), `web/src/lib/events.ts` (op payloads),
`web/src/lib/provenance.ts` (manifest fields on the run page) and
`web/src/components/Badges.tsx` (branch statuses).

- **Run folder** (`experiments/run.py`): `<run>/<scenario>/<condition>/*.eval`,
  `<run>/results-<scenario>-<condition>.json`, `<run>/results.json` (each
  `{"results": [row, ...], ...}`) and `<run>/manifest.json`. cosmos takes the
  scenario and condition from the two folder names.
- **Results rows**: `scenario`, `condition`, `epoch` and `log` (the `.eval`
  path) pair a row with its sample; `seat` and `interventions.off_seat` feed the
  off-seat check. The whole row is shown on the sample page.
- **Info events**: `transcript().info({"event": ..., ...}, source="orbit_audit")`,
  attributed to the enclosing `span(type="branch")`, whose name is the branch id
  (`runner.py`).
  - `orbit_audit_branch` (`branches.py`, `BranchTree`): `version`
    (`BRANCH_EVENT_VERSION`), `action` (`started` or `finished`) plus the
    `BranchNode` fields `id`, `index`, `parent`, `fork_revision`, `fork_reason`,
    `interventions` (the rollback's ops a child applies at its fork: `op` plus
    the op's payload), `status`, `reason`, `revisions`, `output` and `messages`
    (end-of-branch channel posts: `id`, `sender`, `channel_id`, `recipients`,
    `content`, `sequence`).
  - `orbit_audit_intervention` (`evidence.py`, `InterventionRecord`): `version`
    (`EVIDENCE_VERSION`), `op`, `outcome` (`applied` or `rejected`), `sequence`, `op_version`, `boundary`,
    `actor`, `author`, `agents`, `turn`, `revision`, `branch`, `replayed`,
    `reason`, `payload`, `original`, `replacement`, `orbit`, `details`.
  - `orbit_audit_auditor_turn` (`auditor/llm.py`): `branch`, `revision`,
    `boundary`, `agent`, `said`, `ops`, `refused`.
  - `orbit_audit_seat_turn` (`auditor/seat.py`): `branch`, `revision`, `seat`,
    `content`, `tool_calls`, `dropped_calls`.
  - `orbit_audit_timeline_error` (`timeline.py`): `error`, `message`.
  - `l2_steer` (`experiments/l2/steer.py`), with `source="l2"`, not `orbit_audit`, and
    only in L2's own eval log (`<audit>/l2/*.eval`) of a steered audit: `version`
    (`EVENT_VERSION`), `id`, `kind`, `via` (`cosmos` or `local`), `author` (the inbox
    line's author object), `text` (or null), `payload` and `at_step`. One per request the
    audit applied. The mailbox it comes from (`program.json`, `inbox.jsonl`,
    `outbox.jsonl`, schema `cosmos.steer/1`) is Cosmos spec 22 §5, documented in
    `docs/level2-interface.md` §7.1; a change to either needs the matching change there.
  - Any other `event` with `source="orbit_audit"` becomes a parse warning.
  - Values inside these events are hardcoded too: op names (`ops.py`),
    `BranchStatus` values (`branches.py`; `completed` marks the adopted
    branch), `orbit.inspect_message_id` and `orbit.tool_call_id` on
    intervention records (`controller.py`), the `payload` keys `message_id`,
    `recipients`, `content`, `tool_call_id` and `to_revision`, and
    `channel_post` calls (with `arguments.message`) in a generation op's
    `replacement.tool_calls`.
  - The auditor's own model calls run in spans whose type starts with
    `orbit_audit` (`orbit_audit.auditor`), so cosmos does not mistake them for
    the agent's calls when it places chat messages.
- **Authorship labels**: `ChatMessage.metadata["orbit_audit_author"]`
  (`evidence.AUTHOR_KEY`) is `agent:<id>`, `auditor-as:<id>` or `auditor`.
  Untagged assistant messages count as the agent's own output.
- **`manifest.json`** (`build_manifest` in `experiments/run.py`, `"schema":
  "cosmos.manifest/1"`): `sha`, `short_sha`, `branch`, `dirty`, `pr`,
  `scenarios`, `conditions`, `epochs`, `seats`, `models` (per role, plus
  `agents` per scenario), `started_at`, `finished_at`, `argv`, and `live_run`
  (only when the run was streamed with `COSMOS_STREAM=1`: the slug `cosmos live
  open` returned, which makes `cosmos upload` continue that run). cosmos files the
  run by `sha`, `branch` and `pr`, shows `dirty`, `epochs` and the per-role
  `models`, and reads `seats`. Bump `MANIFEST_SCHEMA` for an incompatible
  change too. cosmos does not check it, so the matching cosmos change is still
  required.
- **L2 studies** (`experiments/l2/study.py`, Cosmos spec 12 §12.1): an L2 audit's
  runs name it in their manifest's `study` (`{id, name, experiment, probes}` on an
  experiment, where `probes` gives each variant's probe question, options and `adopted`
  answer; `{id, name, role: "audit", runs: {"exp-<n>": "<slug>"}}` on the audit run), and the
  audit run carries `study/claims.json`, `ledger.json`, `brief.json`, `decisions.json`,
  `proposed.json`, `launches.json` and `review.json`, read by name, `steer.json` and
  `report.json` (`{"steered": n}`) when the audit was steered, and (Cosmos spec 16
  §4.6) `writeup.md`, the images under `figures/` and any other the write-up embeds (at
  their paths in the audit), `report.html` (the audit's `report.md`), `study/writeup.json`
  (`final`, `body_line`, `facts`, `embeds`, `missing`) and `study/figures.json` (per
  comparison figure: `path`, `a`, `b`, `scorer`, `field`, `rounds`, `measure`, `call`,
  `targets`), each only when the audit has it. `annotations.jsonl`
  records (each experiment's, and the audit's) are posted in Cosmos spec 13's shape
  (`experiments.l2.share`). Share reads the runs back, so it needs a full Cosmos token from
  `cosmos login`; an upload-only token gets 403 on its first `GET /api/runs/{slug}`.

cosmos also reads Orbit's own records: `orbit.communication` info events of
kind `sent`, the `CommunicationState:messages` store key and the `experiment`
sample metadata. Check them when you move the Orbit pin.

## Rules

- Never commit `.env`, keys or tokens. `.env`, `.env.*` and `logs/` are
  gitignored; keep them that way.
- No AI or tool attribution in commits, PR descriptions or comments: no
  "Generated with" lines, no `Co-Authored-By` lines for tools, no session links.
- Follow [`docs/spec.md`](docs/spec.md) and
  [`docs/auditor-tests.md`](docs/auditor-tests.md). If a change alters
  specified behaviour, update the spec in the same PR.
- Keep changes focused, match the surrounding code, and add or update tests for
  new behaviour.
