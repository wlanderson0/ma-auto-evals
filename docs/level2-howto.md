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
  (`RUN_CONFIG` in `experiments/l2/experiment.py`), including agents in a variant that
  puts them on several different models (each agent's Orbit `generate_config`).
- It runs until L2 ends the audit or the budget runs out; keep the terminal open and
  the machine awake.
- To stop it early with a report, press Ctrl-C once: the experiment running now
  finishes, then L2 has its draft reviewed and writes its report. Ctrl-C twice stops at
  once (the ledger and report are still written).
- `--time-limit <seconds>` (or `time_limit` in the brief) bounds the audit's runs: once
  only its last 10% is left, no new experiment or epoch starts and L2 concludes in that
  time (it may run a little over, but always ends with a report). A long audit can take hours, so set one when it matters.
- Try a new L2 model or a new kind of brief with a small budget first.

The audit directory holds `writeup.md` (L2's write-up: a results report with tables and
figures, under a table of the audit's facts; start here), `figures/`, `report.md` (L2's claims with their verdicts, its conclusions,
the tables they rest on, every experiment with L2's reason for running it and its variants), `claims.json` (each claim with its
evidence and the checker's reasons), `review.json` (the reviewer's critique and L2's response), `ledger.json` (spend), `trace.jsonl` (every tool call),
`scenarios/` (the code L2 wrote) and `experiments/exp-<n>/` (one per launch).

## 3. Read it critically

- Read `writeup.md` for the story, then check it against `report.md`: the write-up is
  L2's own and unchecked (the reviewer read its draft), the claims are checked.
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
- Read the review in the report: before the audit ended, an adversarial reviewer read
  L2's draft claims and conclusions (with the checker's verdicts) and could query the
  runs; L2 then revised once and said how it answered. A point L2 dismissed is worth
  checking yourself. The draft is in `review.json`.
- Check that a comparison's variants ran in the same experiment, with the same epochs.
- Read a few transcripts (on Cosmos, below) before trusting a measure.

## 4. Share it on Cosmos

In one step, once the audit has ended (or add `--share` to the audit command in step 2,
which does this when it ends):

```bash
uv run python -m experiments.l2.share logs/l2-audit/<name>
```

It exports each experiment, uploads it with your `cosmos login` (each experiment is one
Cosmos run), waits until Cosmos has processed it, and writes the report's claims section
again with links to the runs and messages each claim rests on. It prints each run's
link. Run it again if it stops partway: an experiment already uploaded (its slug is kept
in the audit's `cosmos-runs.json`) is never uploaded twice. `--pr N` files the runs under
a pull request.

The same, step by step. Each experiment of the audit is one Cosmos run:

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
- Then link the report to Cosmos. Give each experiment its run's slug (the end of the
  link the upload printed):

  ```bash
  uv run python -m experiments.l2.cosmos_links logs/l2-audit/<name> \
      --run exp-1=<slug> --run exp-2=<slug>
  ```

  It asks Cosmos (with your `cosmos login`) for each sample's page and each cited
  message's place, saves them as `cosmos.json` in the audit and writes the claims
  section of `report.md` again (the rest is kept as it is): each claim then links to
  every run its counts come from (✓ or ✗ for what the target did there, read as the
  claim's checked call read it) and to each message it quotes. A claim on a count of
  posts (`counts`: a target's uses of a name in its own words) also links, run by run,
  every message that was counted. A claim on probe answers links to the runs only: the
  answer is given in a private interview after the run, not in a message. A slug that is not its
  experiment's run (another commit, start time or set of samples) is refused. The
  report uploaded to Cosmos ("Open report") was made before the links existed, so it
  has none.

The same export works for a fixed grid run by hand with the milestone 1 CLI
(`uv run python -m experiments.l2.experiment --brief ... --out <dir>`): pass `<dir>`.
