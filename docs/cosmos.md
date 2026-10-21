# Sharing runs on cosmos

## What cosmos is

[cosmos](https://cosmos.danhelo.com) is the team's private site for ma-auto-evals
runs. You upload the folder that `experiments.run` writes, and cosmos files it
under its PR and commit. From one link, anyone on the team can read the
transcripts, see who wrote each message (an agent or the auditor), walk the
auditor's branches, diff two branches and check every intervention. It
replaces the shared Drive folder. The code is in
[Danhelo/cosmos](https://github.com/Danhelo/cosmos).

**Access.** Sign in with GitHub. Anyone who can read
[wlanderson0/ma-auto-evals](https://github.com/wlanderson0/ma-auto-evals) gets
in. cosmos checks this when you sign in and again every hour, so losing access
to the repository also ends your access to cosmos. Because the repository is
private, GitHub asks you to grant the `repo` scope; cosmos only uses it to check
that you can read that repository.

| Role | Can |
| --- | --- |
| viewer | read everything, comment |
| uploader | as viewer, plus upload runs, create upload tokens and pin runs |
| admin | as uploader, plus change roles, disable accounts, delete and restore runs, edit or delete any comment, see disk use (**Admin** page) |

New accounts start as uploader. Daniel (`Danhelo`) is the admin.

## First time

1. Open <https://cosmos.danhelo.com>. It sends you to GitHub; approve the
   `cosmos` app.
2. Create a token: **Tokens** in the top bar (or **API tokens** in your user
   menu), at `/settings/tokens`. Give it a name such as `laptop`, pick a scope
   and press **Create token**. Copy it at once: it is shown only once. An
   **upload** token can only upload, which is all the CLI needs; a **full**
   token can do anything you can do in the browser. Revoke tokens on the same
   page.
3. Install the CLI. `Danhelo/cosmos` is private: accept your GitHub invitation
   to it first (ask Daniel if you have none). Then install it with your own
   GitHub access (requires [uv](https://docs.astral.sh/uv/)):

   ```bash
   uv tool install git+https://github.com/Danhelo/cosmos      # https: your git credentials
   uv tool install git+ssh://git@github.com/Danhelo/cosmos    # or ssh: your SSH key
   ```

   `uv tool upgrade cosmos-evals` pulls the latest commit of `main`. If you use
   the GitHub CLI, `gh auth setup-git` lets the https form use your `gh` login.
4. Log in:

   ```bash
   cosmos login --url https://cosmos.danhelo.com
   ```

   It asks for the token (input hidden), checks it with the server and saves the
   URL and token to `~/.config/cosmos/config.json` (mode 0600). Instead, you can
   set `COSMOS_URL` and `COSMOS_TOKEN`; they override the saved config, and
   `--url` / `--token` on a command override both.

## Share a run

From the ma-auto-evals checkout:

```bash
uv run python -m experiments.run --pr <N> --epochs 2   # live: needs OPENROUTER_API_KEY in .env
uv run python -m experiments.run --pr <N> --mock       # or offline, no key
cosmos upload logs/live/<run>
```

`--pr` records the PR in the run's `manifest.json`; without it the runner uses
`$COSMOS_PR`, then `$GITHUB_PR`. The run folder is
`logs/live/<YYYYmmdd-HHMMSS>` (with `-mock` appended for `--mock`), or `--out`;
the runner prints the paths of `manifest.json` and `results.json` at the end.
`manifest.json` records the commit (SHA, branch, whether the working tree was
dirty), the PR, scenarios, conditions, epochs, the seat of each scenario, the
models per role and agent, start and finish times, and the arguments (with
local absolute paths shortened).

`cosmos upload` collects the `.eval`, `.json` and `.html` files in the folder,
skips everything else, scans them for secrets (see
[Troubleshooting](#troubleshooting)), hashes them and sends only the files the
server does not already have. It prints the run link on stdout; progress goes
to stderr.

How the run is filed: PR → commit → run (one upload) → sample → branch.

- PR, commit and branch come from `manifest.json`; `--pr`, `--sha` and
  `--branch` override them. PR and branch are kept on the commit: the first
  upload of a commit that names them decides where it is filed, and a later
  `--pr` or `--branch` does not move it. Ask Daniel to fix a wrongly filed
  commit.
- A sample is one scenario × condition × epoch. Scenario and condition come from
  the folders (`<scenario>/<condition>/*.eval`), the epoch from the Inspect log.
  Each sample gets its row from `results-<scenario>-<condition>.json` or
  `results.json`.
- The run is named `<branch> @ <short sha>` unless you pass `--name`.

The link is the run page, `https://cosmos.danhelo.com/runs/<slug>`. The run is
`queued` at first; the server parses it in the background and the page
refreshes until it is `ready`. Paste the link into the PR description.

If an upload is interrupted, run the same `cosmos upload` again: it resumes that
run and sends only the missing files. Uploading a folder again after it
finished makes a second run (with nothing re-sent).

Useful flags:

- `--dry-run`: check the folder and print the manifest and file list. Uploads
  nothing and needs no token.
- `--name "mfa_rumor l1, CISO framing"`: the run's display name.
- `--pr N`, `--sha S`, `--branch B`: override the manifest (a commit that is
  already filed keeps its PR and branch).

To attach the HTML report, render it into the run folder before uploading; the
run page then has an **Open report** button:

```bash
uv run python -m experiments.report logs/live/<run>/results.json --out logs/live/<run>/report.html
```

A findings file in the folder (a JSON list of `{"title", "body"}` objects, like
`experiments/findings/sweep3.json`) shows as **Findings** on the run page. Old
runs from the Drive folder have no manifest:
`cosmos import-drive <folder> --pr N --sha S` uploads one.

## Reading a run

- **Runs** (home page): every upload, newest first, by PR and commit.
- **Run page**: where the run came from, a grid of its samples, its files,
  findings and comments.
- **Transcript** (a sample's first tab): one branch at a time. Each message is
  marked with who wrote it: the agent, the auditor writing as an agent, or the
  auditor. Auditor operations appear where they happened.
- **Branches** tab: the tree of the auditor's branches. Click one to read it;
  tick two and press **Diff** to compare them.
- **Diff** tab: two branches side by side from the fork point.
- **Interventions** (top bar): every operation the auditor attempted, applied
  or rejected, across runs.
- **Comments and pins**: comment on a run, a sample, a branch or a single
  message. In the transcript, **Comments** opens the panel for the sample, the
  branch and its messages, and each message has its own comment button.
  Uploaders can **Pin** a run so it is never compacted.
- **Permalinks**: **Copy link** on a message opens the transcript at it.

## CI

Not enabled yet. The plan is a workflow in ma-auto-evals that runs the offline
mock suite (`experiments.run --mock`) on every PR, uploads it with an upload
token and comments the run link on the PR. It needs no model key. The setup is
in `docs/ci-upload.md` in Danhelo/cosmos. Until then, upload by hand and put
the link in the PR description.

## Troubleshooting

**Secret refused.** Before uploading, the CLI scans every file, and `.eval`
logs member by member after unzipping, for key patterns: `sk-or-v1-`,
`sk-ant-`, other `sk-…` keys, `AKIA…`, `ghp_`, `github_pat_`,
`gho_`/`ghs_`/`ghu_`/`ghr_`, `hf_`, `AIza…` and PEM private keys. On a match
it uploads nothing and exits 1. The message names the file, the pattern and,
for a log, the member inside it, but never the key:

```
error: results.json: contains something matching secret pattern sk-or-v1-; refusing to upload
```

The manifest is scanned too, and the server scans every file again and refuses
with HTTP 422. If it is a real key, revoke it with its provider before anything
else; `uv run inspect log dump <file>.eval` prints a log as JSON so you can find
where it came from. If it is a false match, ask Daniel. Files whose names look
like secrets (`.env*`, `*.pem`, or `key`, `keys` or `apikey` as a word in a
name that is not `.json` or `.eval`) are refused when they are `.eval`, `.json`
or `.html`, and skipped otherwise:

```
error: .env.json: file name looks like a secret (.env*, *.pem, a key file); refusing to upload
```

**Can't get in.** `<login> does not have access to this cosmos instance` at
sign-in means that GitHub account cannot read wlanderson0/ma-auto-evals. Sign
in with the account that can, or ask William for access. If
`uv tool install` fails with `Repository not found` or cannot read a username,
you have not accepted the Danhelo/cosmos invitation or git has no GitHub
credentials: run `gh auth setup-git`, or use the ssh form.

**Dirty working tree.** `manifest.json` has `"dirty": true` when the checkout
had uncommitted changes or untracked files as the run started (`logs/` and
`.env` are gitignored and do not count). The run page then shows **Working
tree: dirty (uncommitted changes)**: the code that ran is not exactly the
recorded commit. For a run others will rely on, commit and push first, then
run again.

**No `manifest.json`.** Older runs have none. `cosmos upload` then asks git in
the current directory for the commit, branch, commit message and dirty flag
(tracked files only), so run it from the ma-auto-evals checkout at the commit
the run was made from, and pass `--pr N` (the CLI does not read `$COSMOS_PR`).
With no git checkout and no `--sha` it warns `no commit sha` and the run has no
commit. `--dry-run` shows what it will send.

**Parse failure.** The upload is kept even when parsing fails. The run page
shows **Parsing failed.** with the error, and **Files** still lists every
uploaded file. If only some events could not be read, the run is `ready` with a
parse warnings notice, and branches or authorship may be incomplete. Send
Daniel the link: once the parser is fixed he re-parses the stored files
(`cosmos reindex --run <slug>` on the server), so you do not upload again.

**Other errors.** `HTTP 401`: the token was revoked or your access check
failed; sign in on the site again, or make a new token and run `cosmos login`
again. `HTTP 403 … requires uploader role`: ask Daniel for the uploader role.
`HTTP 507`: the server disk is nearly full and uploads are paused. A run marked
**stalled**: the upload was interrupted; run the same `cosmos upload` again.

**Who to ask.** Daniel (GitHub `Danhelo`) runs cosmos: access, roles, deleted
runs, parse failures and false secret matches. Bugs can also go to the issues
of Danhelo/cosmos.
