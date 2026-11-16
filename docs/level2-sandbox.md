# Level 2: sandboxing L2-written Python

Status: design for review, 2026-11-16. No code yet. Answers the two open questions in
[`level2-interface.md`](level2-interface.md) §12, "Where does L2's code workspace run?" and
"Where does scenario code run?".

## Summary

Run every piece of L2-written Python through one launcher that wraps the command in an OS
sandbox: no network, and only the folders it is given. Use Docker where a daemon exists (a
developer's Mac, CI) and bubblewrap where it does not (cloud containers). All the places
L2's code runs in a subprocess are already `subprocess` calls, so the change is a wrapper
around them, not a new architecture.

Two findings make this more urgent than §12 suggests. Both come from running today's
`run_python` (`main` at `5f8a0fa`) in a cloud container whose OpenRouter key is a network
secret, attached to requests outside the machine:

- **Blanking the keys protects nothing there.** With `OPENROUTER_API_KEY` empty, a plain
  `urllib` request to `openrouter.ai/api/v1/key` with the header `Bearer x` returned 200,
  because the secret is attached for any process. L2-15 ("a model call made from it fails")
  does not hold in that environment.
- **The same call read `docs/level2-bench-rubric.md`**, which holds the bench answers. The
  rubric's manual trace check is the only guard today.

The work splits into three phases. Phase 1 (the analysis workspace and the scenario check)
is small and closes both findings. Phase 2 confines scenario code in live epochs. Phase 3, a
metering proxy, comes later.

## Where L2's code runs today

L2-written Python runs in four places. None is confined to a folder, and none has its
network cut.

| Place | Code | Has the key? | Files | Network |
| --- | --- | --- | --- | --- |
| `run_python` (analysis) | `workspace.run_python`: `python -I -c` in the audit folder, 60 s limit | `*_API_KEY` set empty | Anything the user can read or write, including the repo's `.env` | Open |
| `write_scenario` check | `written.check_code`: `python -m experiments.l2.written --check` from the repo root, 300 s limit | `*_API_KEY` set empty | Same | Open |
| Live epoch | `experiment.launch_subprocess`: imports the written scenario and runs its tools and outcome function | Yes, the agents need it | Same | Open |
| The L2 process itself | `written.load` imports each written module in-process (`audit.py`, `experiment.py`, `cosmos.py`) | Yes | Same | Open |

What can go wrong, most likely first:

1. **Bench contamination.** L2 reads the rubric, `experiments/scenarios.py`, tests or earlier
   audits and "finds" the known answer.
2. **Spend the ledger does not see.** Code calls a model directly. On a laptop it needs the
   key, which it can read from `.env`. With a network secret it needs nothing.
3. **Injected instructions.** Transcript text from a run steers L2 into writing code that
   reads or sends files. L2 is not hostile, but it reads untrusted agent output all day.
4. **Damage by accident.** A script overwrites logs, another audit or the repo.

## What the sandbox must guarantee

1. **Files are an allowlist.** Code sees the folders it is given and the Python environment,
   and nothing else: no `.env`, no home folder, no repo docs or tests, no other audits.
2. **Writes go to one place.** Logs and sources are read-only. Analysis code writes only to a
   scratch folder inside the audit.
3. **No network for analysis and checks.** For live epochs, only the model provider is
   reachable.
4. **Limits.** Time (as now), memory and process count, and nothing outlives the call.
5. **Fail closed.** A live audit refuses to start if no sandbox is available, unless an
   explicit opt-out is passed. The audit record says which sandbox ran, so a bench run made
   without one visibly does not count.
6. **Works where audits run.** macOS in a terminal, a Linux cloud container, and CI offline.

Not a goal: defending against a kernel or container escape. The adversary is a confused or
injected model, not an attacker with exploits.

## Options

The recommendation is a command wrapper with two backends, Docker and bubblewrap. Both show
the code only what is mounted, so one policy (mounts, network, limits) describes both.

| Option | Mac | Cloud container | Files allowlist | Verdict |
| --- | --- | --- | --- | --- |
| Docker container per call (`docker run --network none`, read-only mounts) | Needs Docker Desktop, OrbStack or colima | No: the client is installed but no daemon runs (checked) | Yes, by mounts | **Backend 1** |
| bubblewrap (`bwrap --unshare-all`, read-only binds) | No, Linux only | Yes after `apt install bubblewrap`; network and user namespaces work there (checked with `unshare`) | Yes, by binds | **Backend 2** |
| Inspect's `sandbox="docker"` on `l2_task` | Same as Docker | No | Yes | Not now, see below |
| macOS Seatbelt (`sandbox-exec`) | Yes, no install | No | Possible, but Python needs a long list of system paths | Not needed while Docker is required on the Mac |
| Anthropic's `sandbox-runtime` (Seatbelt plus bubblewrap in one tool) | Yes | Probably | Unverified: it may deny listed read paths, not allowlist them | Worth a look before building backend 2 |
| Hosted sandbox (E2B, Modal) | Yes | Yes | Yes | No: logs leave the machine, another account and key |
| Pyodide/WASM | Yes | Yes | Yes | No: `inspect_ai.log` and the repo's code won't run in it |

**Why not Inspect's own sandbox, as §12 suggests.** It fits `run_python` but not the other
places: the scenario check and the epochs are subprocesses started from inside a tool,
outside any sample's sandbox. It also needs Docker, which cloud containers lack. A wrapper
covers all of them the same way. If Inspect's sandbox is wanted later, the Docker backend
can become one without changing the policy.

## Design: the launcher and the analysis workspace (phase 1)

A new module `experiments/l2/sandbox.py` turns a command and a policy into the command to
run. The call sites keep their `subprocess` code and pass their argv through it.

```python
@dataclass(frozen=True)
class Policy:
    read: tuple[Path, ...]      # mounted read-only
    write: tuple[Path, ...]     # mounted read-write
    cwd: Path
    network: Literal["none", "provider"] = "none"
    env: Mapping[str, str] = {}  # the whole environment; nothing is inherited
    memory_mb: int = 2048
    pids: int = 128

def wrap(command: Sequence[str], policy: Policy, backend: str) -> list[str]: ...
def available() -> str | None:   # "docker", "bwrap" or None
```

**Policy for `run_python`**

- Read: the audit folder and each folder in `L2_SOURCES`.
- Write: `<audit>/workspace/` only, which is also the working directory. Today the working
  directory is the audit folder itself, so L2's code could rewrite `ledger.json` or the logs.
  Relative paths change from `experiments/...` to `../experiments/...`; the tool description
  says so.
- Network: none.
- Environment: built from nothing (`PATH`, `HOME` set to the scratch folder, `L2_SOURCES`).
  No inherited variables, so the key-blanking code and its `.env` search become unnecessary
  inside a sandbox.
- Python: an interpreter with `inspect_ai` and pandas but **not the repository**. With Docker
  that is a small image built from the lockfile (`experiments/l2/sandbox/Dockerfile`, tagged
  with the lockfile hash). With bubblewrap it is the existing virtualenv bound read-only,
  without the repo's source folders.

**L2's analysis code cannot import the repo's helpers** (`experiments.l2.results` and
others). Importing the package would put `experiments/` in the sandbox, and
`experiments/scenarios.py` gives away bench answers. This matches the bench rubric, under
which a run that imports `experiments` does not count. If L2's analysis visibly suffers, a
small separate package of safe loaders can be added to the sandbox later.

**Policy for the `write_scenario` check.** Same, plus the repo's `src/` and `experiments/`
read-only, since the check imports them. `docs/`, `tests/`, `logs/` and `.env` stay out. The
check's code can still read `experiments/scenarios.py`; that only matters for the bench, and
the check's output goes back to L2 as pass or fail reasons, not file contents.

**Choosing the backend.** `--sandbox auto|docker|bwrap|none` on `experiments.l2.audit`,
default `auto`. `auto` takes Docker if a daemon answers, else bubblewrap, else stops a
`--live` audit with a message that names the fix. `--mock` runs fall back to today's
subprocess, so tests and CI stay offline and fast. `none` is today's behaviour, for a
deliberate opt-out. The choice is written to the audit's `ledger.json` and shown in the
report header.

**Cost.** An estimate, not measured: a `docker run` adds about a second per call, and
bubblewrap a few milliseconds. `run_python` already allows 60 s, so neither changes how L2
works.

## Design: scenario code in live epochs (phases 2 and 3)

This is the harder half, because the epoch process must call models while it runs L2's
code. A sandbox can confine its files and narrow its network, but as long as the process
holds a working route to the provider, L2's code can use it too.

**Phase 2: confine the epoch**

- **Run each epoch through the launcher.** Read: the repo's `src/` and `experiments/`, the
  brief and the audit's `scenarios/`. Write: the experiment's output folder. No `.env`; the
  key is passed in the environment for that one process.
- **Network `provider`.** Only the model provider is reachable. With Docker, the epoch
  container sits on an internal network whose only exit is a small forwarding proxy that
  allows `openrouter.ai:443`. In a cloud container the environment's own proxy already limits
  hosts to its allowlist, so bubblewrap shares the host network there and adds nothing.
- **Stop importing written scenarios in the L2 process.** `written.load` runs each module's
  top level and `make({})` inside the process that holds the key and has no sandbox. Replace
  it with a sandboxed call that returns a description of the scenario as JSON (agents,
  models, probes, tool names, hash), which is all the L2 process needs to build variants and
  prices. Not every caller has been traced, so this is the item most likely to grow.
- **Import check at `write_scenario`.** Reject code that imports outside an allowlist
  (`experiments.scenarios`, `orbit`, `inspect_ai.tool`, standard library data modules) or
  names `socket`, `urllib`, `httpx`, `requests`, `subprocess`, `os.environ`. This is easy to
  get around on purpose, so it is a tripwire for honest mistakes, not a boundary.

After phase 2, written scenario code cannot touch files or other hosts. A direct model call
from it is still possible and goes uncounted until phase 3. Checking the ledger against the
provider's own usage figure after each epoch was considered and dropped: it only detects
after the fact, and gives false alarms when several audits share one key.

**Phase 3 (later): the key never enters the epoch.** The forwarding proxy also adds the
`Authorization` header and counts tokens per request. The epoch holds a placeholder key, and
the ledger reads its numbers from the proxy, so there is no uncounted call left. This is a
network secret rebuilt locally with a meter on it. It is a real piece of software to
maintain, so it comes after phases 1 and 2.

## Phases, tests and spec changes

| Phase | What ships | Closes | Size (estimate) |
| --- | --- | --- | --- |
| 1 | `sandbox.py` with Docker and bubblewrap backends; `run_python` and `check_code` go through it; `--sandbox` flag; backend recorded in the ledger | Bench contamination, key and file reads, uncounted calls from analysis code (including with a network secret) | One PR, a few hundred lines with tests |
| 2 | Epochs through the launcher; provider-only network; written scenarios described out of process; import check | File access and other hosts from scenario code | Two or three PRs |
| 3 | Key-injecting, metering proxy | Uncounted calls from scenario code | Later |

**Tests**

- L2-15 gains its "files" half: from `run_python`, opening the repo's `.env`,
  `docs/level2-bench-rubric.md` and the home folder fails, and a write outside `workspace/`
  fails.
- L2-15's "a model call fails" is tested at the socket: a connection to any host fails. The
  current test only shows that Inspect refuses an empty key.
- A new test that a `--live` audit with no backend stops before spending.
- Unit tests on `wrap()` compare the argv for each backend and need no Docker. The tests that
  really start a sandbox carry a new `sandbox` marker and skip when no backend is present; CI
  on Ubuntu has Docker, so they run there.

**Spec.** With phase 1, `level2-interface.md` §6 (workspace), §3.5 and §7 (keys) and the two
§12 questions are rewritten as decisions, and `level2-bench-rubric.md` replaces its manual
trace check with "the audit record shows a sandbox backend". Nothing in the contract cosmos
reads changes; showing the backend on the study page would be a small cosmos change later.

**Cloud containers** need `bubblewrap` added to the environment's setup script. The install
has not been tried.

## Decisions and open questions

Decided (2026-11-16):

- Docker is required for live audits on a Mac.
- `--sandbox auto` fails closed for live audits from the first PR, with `--sandbox none` as
  the way out.
- L2's analysis code cannot import the repo's helpers.
- No spend reconciliation; the metering proxy comes later.

Open:

- Do audits run anywhere other than a Mac with Docker and a Linux cloud container? That
  decides whether two backends are enough.
- Is `sandbox-runtime` a better base for the bubblewrap backend than calling `bwrap`
  directly?
