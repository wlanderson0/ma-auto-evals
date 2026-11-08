# Sharing runs on cosmos

## What cosmos is

[cosmos](https://cosmos.danhelo.com) is the team's private site for ma-auto-evals
runs. You upload the folder that `experiments.run` writes, and cosmos files it
under its PR and commit. From one link, anyone on the team can read the
transcripts, see who wrote each message (an agent or the auditor), walk the
auditor's branches, diff two branches and check every intervention. It
replaces the shared Drive folder. The code is in
[Danhelo/cosmos](https://github.com/Danhelo/cosmos).

**Access.** Sign in with GitHub. Runs live in projects, and the team's runs are
in the `ma-auto-evals` project, which is linked to
[wlanderson0/ma-auto-evals](https://github.com/wlanderson0/ma-auto-evals):
anyone who can read the repository is a member. cosmos checks this when you sign
in and again every hour, so losing access to the repository also ends your
access to the project. For now GitHub asks you to grant the `repo` scope; cosmos
only uses it to check that you can read the linked repository. Once the cosmos
GitHub App is set up, sign-in asks for no scope.

Roles are per project:

| Role | Can |
| --- | --- |
| viewer | read everything, comment, highlight |
| uploader | as viewer, plus upload runs, pin runs, make share links and add agents |
| admin | as uploader, plus manage members and project settings, delete and restore runs, edit or delete any comment |

Members who come from the repository start as uploader. Daniel (`Danhelo`) is a
project admin and the site admin (server settings, disk use, project requests).

## First time

1. Open <https://cosmos.danhelo.com>. It sends you to GitHub; approve the
   `cosmos` app.
2. Create a token: in your user menu, **Settings → API tokens**
   (`/settings/tokens`). Give it a name such as `laptop`, pick a scope
   and press **Create token**. Copy it at once: it is shown only once. An
   **upload** token can only upload, which is all `cosmos upload` needs; a
   **full** token can do anything you can do in the browser. L2's share
   (`experiments.l2.share`) also reads its runs back, so it needs a full token
   saved with `cosmos login`: an upload-only token stops at its first
   `GET /api/runs/{slug}` with 403. Revoke tokens on the same page.
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
   `--url` / `--token` on a command override both. If you can upload to more
   than one project, add `--project ma-auto-evals` (to `cosmos login`, once, or
   to each upload). `cosmos doctor` checks the URL, the token and where an
   upload would go.

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

- **Runs** (the project's home, `/p/ma-auto-evals`): every upload, newest
  first, by PR and commit, each with a health badge (clean, notice or dirty).
- **Run page**: where the run came from, its health and the comparison with
  its baseline run, a grid of its samples, its files, findings and comments.
- **Transcript** (a sample's first tab): one branch at a time. Each message is
  marked with who wrote it: the agent, the auditor writing as an agent, or the
  auditor. Auditor operations appear where they happened.
- **Branches** tab: the tree of the auditor's branches. Click one to read it;
  tick two and press **Diff** to compare them.
- **Diff** tab: two branches side by side from the fork point.
- **Exposure** tab: which agents saw each post the auditor wrote or rewrote,
  and at which revision. **Timeline** shows the sample in order of time.
- **Compare with…** on a sample opens it beside another sample or branch (the
  baseline run's same cell is offered first).
- **Interventions** (in the project's tabs): every operation the auditor
  attempted, applied or rejected, across runs.
- **Highlights**: select text in a transcript and press **Highlight** (or `h`)
  to save a quote with tags and a note. The project's **Highlights** tab
  collects them and exports Markdown, CSV or JSON lines.
- **Comments and pins**: comment on a run, a sample, a branch, a message or a
  range of messages. In the transcript, **Notes** holds the comments and
  highlights of the branch you are reading. Mention someone with `@login`; the
  bell in the header and `/inbox` show your mentions and replies. Uploaders can
  **Pin** a run so it is never compacted.
- **Links**: **Copy link** on a message opens the transcript at it; click a
  message's `#` and Shift-click another to link a range. **Share** makes an
  expiring read-only link for someone without an account, if a project admin
  has turned sharing on.
- **Search** in the header searches every message, with filters by scenario,
  condition and author. Press `?` for the keyboard shortcuts.
- **Agents**: a coding agent (Claude Code, Codex, ChatGPT and others) can read
  runs through cosmos's MCP server, and agents added on the project's
  **Settings → Agents** tab answer `@`-mentions in comments. See the
  [cosmos README](https://github.com/Danhelo/cosmos#readme).

## CI

Not enabled yet. The plan is a workflow in ma-auto-evals that runs the offline
mock suite (`experiments.run --mock`) on every PR, uploads it and puts the run
link on the PR. It needs no model key. Once the cosmos GitHub App is installed
on this repository, the workflow uploads with GitHub's own OIDC token, with no
secret to store; until then it would need an upload token as a repository
secret. The setup is in `docs/ci-upload.md` in Danhelo/cosmos. Until then,
upload by hand and put the link in the PR description.

## Troubleshooting

**Secret refused.** Before uploading, the CLI scans every file, and `.eval`
logs member by member after unzipping, for key patterns: `sk-or-v1-`,
`sk-ant-`, other `sk-…` keys, `AKIA…`, `ghp_`, `github_pat_`,
`gho_`/`ghs_`/`ghu_`/`ghr_`, `hf_`, `AIza…`, `css_`, PEM private keys and
cosmos's own tokens (`cos_`, `cosa_`, `coa_`, `cor_`, `cosw_`). On a match
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

**Can't get in.** If you can sign in but do not see the `ma-auto-evals`
project (or sign-in says your login has no access to any cosmos project), that
GitHub account cannot read wlanderson0/ma-auto-evals. Sign in with the account
that can, or ask William for access. Do not request a new project for it. If
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
again. `HTTP 403 … requires the uploader role`: ask a project admin (Daniel)
for the uploader role.
`HTTP 507`: the server disk is nearly full and uploads are paused. A run marked
**stalled**: the upload was interrupted; run the same `cosmos upload` again.

**Who to ask.** Daniel (GitHub `Danhelo`) runs cosmos: access, roles, deleted
runs, parse failures and false secret matches. Bugs can also go to the issues
of Danhelo/cosmos.
