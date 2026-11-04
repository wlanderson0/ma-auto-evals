# Running an L2 audit and sharing it on Cosmos

How to give L2 a research question, run it, and share what it did. The design is in
[`level2-interface.md`](level2-interface.md); Cosmos itself in [`cosmos.md`](cosmos.md).

## Once

- `uv sync` in the repo.
- An OpenRouter key in a gitignored `.env` at the repo root:
  `OPENROUTER_API_KEY=...`. Live runs spend money on it.
- The `cosmos` CLI, logged in ([`cosmos.md`](cosmos.md), "First time"). If
  `uv tool install git+https://github.com/Danhelo/cosmos` cannot read a username, use
  `uv tool install git+ssh://git@github.com/Danhelo/cosmos`.

## 1. Give L2 a question

The shortest brief is a question and a budget, straight on the command line (step 2):
`--question "Does ...?" --budget 3`. L2 decides everything else, including whether to
reuse an existing scenario or write its own; the audit keeps it as its `brief.md`.

To fix more (the models, the measure, which scenarios it may use, variants to run),
write a brief file instead and pass it with `--brief`. It is a Markdown file: a YAML
header, then the question in plain words ([`level2-interface.md`](level2-interface.md)
§2 lists every parameter).

```markdown
---
budget_usd: 3
base_scenarios: []
---
Does ...? (the question, in plain words)
```

- `budget_usd` (required) is a hard limit on everything: L2's own calls and its runs.
  `run_budget_usd` optionally caps the runs inside it.
- `base_scenarios: []` names no existing scenario, so L2 writes its own; leave the key
  out to let it start from any seed scenario, or list the ones it may use.
- No YAML comments (`#`) and no hints you don't mean L2 to have: the audit keeps a copy
  of the brief that L2 can read.
- Briefs live in `experiments/l2/briefs/`.

## 2. Run L2

```bash
uv run python -m experiments.l2.audit --question "Does ...?" --budget 3 --live \
    --model openrouter/anthropic/claude-opus-5.5 --out logs/l2-audit/<name>
# or, with a brief file:
uv run python -m experiments.l2.audit --brief experiments/l2/briefs/<brief>.md --live \
    --model openrouter/anthropic/claude-opus-5.5 --out logs/l2-audit/<name>
```

- `--model` is L2's own model; it must have a row in `experiments/l2/cost.py`
  (`PRICES`), or the audit refuses to start. The agents' models are L2's choice (the
  brief can fix them); written scenarios that name none get `openrouter/openai/gpt-4.1-mini`.
- Every model of a run gets a 1500-token reply cap and reasoning effort "high"
  (`RUN_CONFIG` in `experiments/l2/experiment.py`), except agents in a variant that
  puts them on several different models, which run without them; each experiment's
  `decisions.json` lists those variants (`without_run_config`).
- It runs until L2 ends the audit or the budget runs out; keep the terminal open and
  the machine awake. `--time-limit <seconds>` ends it cleanly earlier.
- Try a new L2 model or a new kind of brief with a small budget first.

The audit directory holds `report.md` (L2's claims with their verdicts, its conclusions,
the tables they rest on, every variant and experiment), `claims.json` (each claim with its
evidence and the checker's reasons), `ledger.json` (spend), `trace.jsonl` (every tool call),
`scenarios/` (the code L2 wrote) and `experiments/exp-<n>/` (one per launch).

## 3. Read it critically

- Start from the claims at the top of `report.md`. Each was read again from the logs
  (spec §6.4): only *checked* claims are findings; a *lead* falls short (e.g. not told
  apart from chance); *contradicted* and *invalid* ones are wrong as stated; *unchecked*
  ones (kind `other`, e.g. a result of L2's own code) and anything stated only in the
  prose need reading by hand, against the call or post they cite.
- To check a claim yourself, use "Where the cited evidence is" under the claims: for
  each cited call, its row in `trace.jsonl` and the eval logs it read (a path pattern
  per variant); for each cited post, its log, message id, sender and text. On Cosmos,
  samples are filed by the variant's built name (`<base>--<variant>`), condition and
  epoch; find a post there by its text, since Cosmos numbers messages itself.
- Check that a comparison's variants ran in the same experiment, with the same epochs.
- Read a few transcripts (on Cosmos, below) before trusting a measure.

## 4. Share it on Cosmos

Each experiment of the audit is one Cosmos run:

```bash
for d in logs/l2-audit/<name>/experiments/exp-*; do
  uv run python -m experiments.l2.cosmos "$d" --out "logs/cosmos/<name>/$(basename $d)" --pr <N>
done
for d in logs/cosmos/<name>/exp-*; do
  cosmos upload "$d" --name "<name> $(basename $d)"
done
```

- The export never changes the audit and never uploads; it prints the upload command.
- `--pr` files the run under a pull request; without it, under the commit the
  experiment ran at (recorded in its `decisions.json`).
- `cosmos upload <folder> --dry-run` checks a folder without uploading.
- An `HTTP 401` means the Cosmos sign-in has lapsed: sign in again on
  <https://cosmos.danhelo.com> (or `cosmos login` with a new token) and rerun.
- Each upload prints the run's link; put the links in the PR or the write-up.

The same export works for a fixed grid run by hand with the milestone 1 CLI
(`uv run python -m experiments.l2.experiment --brief ... --out <dir>`): pass `<dir>`.
