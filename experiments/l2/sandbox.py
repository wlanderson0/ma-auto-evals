"""A sandbox for the Python L2's model writes (spec ``docs/level2-interface.md`` §6, §12;
Cosmos spec 20, rule R18; Cosmos spec 22 §6, package 22-S).

Two backends behind one interface, picked by platform (``detect``): ``bwrap``
(bubblewrap, Linux) and ``sandbox-exec`` (a Seatbelt profile, macOS). Nothing else is
tried, and there is no fallback to running outside: when the sandbox cannot start, the
caller gets ``SandboxFailed`` and nothing ran. The policy is the same for both:

- no network at all (bwrap unshares the network namespace; the Seatbelt profile has no
  network rule, so its ``deny default`` holds);
- reads only what the interpreter needs (the system library directories,
  ``sys.base_prefix`` and ``sys.prefix``, each bound by itself, never the checkout or the
  home directory around them) and the read paths the caller names;
- writes only the one directory the caller names, and a private scratch that does not
  outlive the run (bwrap mounts it on ``/tmp``, or on ``ALT_SCRATCH`` when a path of the
  run lies under ``/tmp``, so a write beside that path fails rather than landing in it);
- an environment built from nothing (``environment``): no variable of the caller
  reaches the code, so no key, token or ``COSMOS_*``;
- no descriptor but stdin (``/dev/null``), stdout, stderr and the ready descriptor.

Every path is resolved (macOS ``/tmp`` and ``/var`` are links into ``/private``). On
macOS the paths reach the profile as ``-D`` parameters, never as text inside it, so a path
holding a quote or a backslash cannot change the profile.

**Started or failed.** The sandboxed command is a fixed bootstrap (``BOOTSTRAP``) that
writes one byte to an inherited pipe, closes it and only then runs the caller's
arguments in a fresh interpreter. No byte means the sandbox or the interpreter did not
start: ``SandboxFailed``, with the backend's output.

After each run, anything in the write directory that is not a regular file or a
directory (a symbolic link, a fifo, a socket, a device node) is removed (``scrub``), so
code outside the sandbox that later reads the directory never follows a link planted
there.

``check()`` is the proof on a given machine: it runs a probe inside the sandbox and
passes only when every property in ``PROPERTIES`` holds. ``python -m
experiments.l2.sandbox --check [--json]`` runs it (Cosmos spec 23's ``sandbox_check``):
exit 0 and one line naming the kind when it passes, exit 1 and one line per failed
property when it does not, exit 2 for arguments it cannot read. The macOS backend is new
and is proven on each machine by that command (and by CI on ``macos-latest``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("bwrap", "sandbox-exec")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
"""A fixed path, so no ``PATH`` entry can stand in for it."""
BOOTSTRAP = ("import os,sys;fd=int(sys.argv[1]);os.write(fd,b'1');os.close(fd);"
             "os.execv(sys.argv[2],sys.argv[2:])")
"""Run inside the sandbox before anything else: the ready byte, then the caller's
interpreter and arguments (``python -I -c BOOTSTRAP <fd> <python> <args...>``)."""
LINUX_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
"""System directories bound read-only when they exist (a link, as on a merged ``/usr``,
stays a link)."""
LINUX_ETC = ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/fonts",
             "/etc/localtime")
"""The minimum of ``/etc``: the dynamic linker's files, fonts and the time zone."""
MAC_READ = ("/usr", "/System", "/Library", "/private/var/db/dyld",
            "/private/var/db/timezone")
"""What a macOS interpreter reads besides itself: system libraries and frameworks, the
dyld shared cache and the time zone data."""
MAC_READ_FILES = ("/private/etc/localtime", "/dev/urandom", "/dev/random", "/dev/zero")
MAC_SERVICES = ("com.apple.system.opendirectoryd.libinfo", "com.apple.system.logger",
                "com.apple.logd", "com.apple.system.notification_center")
"""Mach services libc may look up (user and group lookups, logging, notifications);
none of them reads a file or the network for the code."""
SCRATCH_DIRS = ("home", "cache", "config", "data", "mpl")
MAX_LINKS = 40
"""The most links followed to an interpreter (a loop of links ends there)."""
ALT_SCRATCH = "/.l2-tmp"
"""bwrap's scratch when a path of the run lies under ``/tmp`` (e.g. an audit there): a
scratch mounted on ``/tmp`` would hold that path's parents, and a write beside it would
land in the scratch instead of failing."""
PROPERTIES = ("starts", "no-local-tcp", "no-udp", "no-outbound", "hidden-files",
              "reads-sources", "writes-outside-fail", "writes-inside-work",
              "clean-environment")
"""What ``check`` proves, in order: the sandbox starts and Python imports its standard
library; a TCP connection to a listener on 127.0.0.1 fails; a UDP datagram to one never
arrives; an outbound connection fails; files outside every allowed path cannot be read
or listed; the read paths can be read; a write outside the write directory fails; a
write inside it works and is seen outside; no variable of the caller is visible."""
CHECK_TIMEOUT = 60
OUTBOUND = "1.1.1.1"
"""An address on the internet the probe tries to reach (port 443; nothing is sent)."""


class SandboxFailed(Exception):
    """The sandbox did not start (or cannot on this machine), so nothing ran."""


@dataclass(frozen=True)
class Ran:
    """What a sandboxed run gave: its exit code (``None`` when it was stopped at the time
    limit) and output (``stderr`` is ``""`` when it was merged into ``stdout``)."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


@dataclass(frozen=True)
class Plan:
    """One run's paths, resolved: the interpreter, the directories it lives in
    (``runtime``), the caller's read paths, the one write directory, the working
    directory, the environment's extra variables, the directories whose names alone
    may be listed (``lists``), whether the code may start processes (``children``) and
    the links the interpreter is reached through (``links``: where each is, and what it
    holds)."""

    python: str
    runtime: tuple[str, ...]
    reads: tuple[str, ...]
    write: str
    cwd: str
    extra: Mapping[str, str] = field(default_factory=dict)
    lists: tuple[str, ...] = ()
    children: bool = True
    links: tuple[tuple[str, str], ...] = ()


def _real(path: str | os.PathLike) -> str:
    return os.path.realpath(os.fspath(path))


def interpreter() -> tuple[str, tuple[str, ...]]:
    """The interpreter to run inside (the virtualenv's own, so it finds its packages) and
    the directories it needs: ``sys.base_prefix`` and ``sys.prefix``, resolved."""
    prefix, exe = Path(sys.prefix), Path(sys.executable)
    try:
        python = os.path.join(_real(prefix), exe.relative_to(prefix))
    except ValueError:
        python = os.path.join(_real(exe.parent), exe.name)
    runtime = tuple(dict.fromkeys(_real(p) for p in (sys.base_prefix, sys.prefix)))
    return python, runtime


def crossed(path: str | os.PathLike) -> list[tuple[str, str]]:
    """Every link crossed in resolving ``path``, in order: where it is and what it
    holds."""
    found: list[tuple[str, str]] = []
    todo = os.path.abspath(os.fspath(path))
    while len(found) < MAX_LINKS:
        walk, parts = "/", Path(todo).parts[1:]
        for i, part in enumerate(parts):
            walk = os.path.join(walk, part)
            if os.path.islink(walk):
                target = os.readlink(walk)
                found.append((walk, target))
                todo = os.path.normpath(os.path.join(os.path.dirname(walk), target,
                                                     *parts[i + 1:]))
                break
        else:
            break
    return found


def interpreter_links(python: str) -> tuple[tuple[str, str], ...]:
    """The links ``python`` and its base installation are reached through. A virtualenv's
    interpreter is a link to the base one, and its ``pyvenv.cfg`` names the base by a
    path that may itself cross a link (uv's ``cpython-3.13-...`` to ``cpython-3.13.9-...``),
    so both the program and its standard library are found only where those links
    exist."""
    paths = (python, sys.base_prefix, sys.base_exec_prefix)
    return tuple(dict.fromkeys(link for path in paths for link in crossed(path)))


def _refuse_broad(path: str) -> None:
    """A read or write path must not be the root, the home directory or a directory
    above it: that would show the files the sandbox hides."""
    home = _real(Path.home())
    if path == "/" or home == path or home.startswith(path.rstrip("/") + "/"):
        raise SandboxFailed(f"refused: {path} would expose the home directory or the root")


def plan(cwd: Path, reads: Sequence[Path], write: Path,
         extra: Mapping[str, str] | None = None, lists: Sequence[Path] = (),
         children: bool = True) -> Plan:
    """The resolved ``Plan`` for a run in ``cwd`` that reads ``reads`` and writes
    ``write`` (which must exist)."""
    python, runtime = interpreter()
    resolved = tuple(dict.fromkeys(_real(p) for p in reads))
    target = _real(write)
    if not os.path.isdir(target):
        raise SandboxFailed(f"the write directory {target} does not exist")
    for path in (*resolved, target):
        _refuse_broad(path)
    return Plan(python=python, runtime=runtime, reads=resolved, write=target,
                cwd=_real(cwd), extra=dict(extra or {}),
                lists=tuple(dict.fromkeys(_real(p) for p in lists)), children=children,
                links=interpreter_links(python))


def environment(scratch: str, python: str,
                extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The sandbox's whole environment, built from nothing: ``scratch`` is its private
    temporary directory (``/tmp`` inside bwrap)."""
    utf8 = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    return {"PATH": f"{os.path.dirname(python)}:/usr/bin:/bin",
            "HOME": f"{scratch}/home", "TMPDIR": scratch, "LANG": utf8, "LC_ALL": utf8,
            "MPLBACKEND": "Agg", "MPLCONFIGDIR": f"{scratch}/mpl",
            "XDG_CACHE_HOME": f"{scratch}/cache", "XDG_CONFIG_HOME": f"{scratch}/config",
            "XDG_DATA_HOME": f"{scratch}/data", "PYTHONDONTWRITEBYTECODE": "1",
            **dict(extra or {})}


# ------------------------------------------------------------------- bwrap
def linux_system() -> list[tuple[str, str | None]]:
    """The system paths that exist here, each with its link target when it is a link."""
    found = []
    for path in (*LINUX_SYSTEM, *LINUX_ETC):
        if os.path.islink(path):
            found.append((path, os.readlink(path)))
        elif os.path.exists(path):
            found.append((path, None))
    return found


def _outermost(paths: Sequence[str], inside: Sequence[str]) -> list[str]:
    """``paths`` without those already under one of ``inside``, parents first."""
    def under(path: str, root: str) -> bool:
        return path == root or path.startswith(root.rstrip("/") + "/")

    kept = [p for p in paths if not any(under(p, root) for root in inside)]
    return sorted(dict.fromkeys(kept), key=lambda p: (p.count("/"), p))


def bwrap_scratch(run: Plan) -> str:
    """Where bwrap mounts the private scratch: ``/tmp``, unless a path of the run lies
    under it (``ALT_SCRATCH`` then)."""
    paths = (*run.runtime, *run.reads, run.write, run.cwd, run.python)
    return ALT_SCRATCH if any(p == "/tmp" or p.startswith("/tmp/") for p in paths) else "/tmp"


def bwrap_args(binary: str, run: Plan, args: Sequence[str], ready: int,
               system: Sequence[tuple[str, str | None]]) -> list[str]:
    """The ``bwrap`` command line for ``run``: an empty root with the system paths and
    the runtime read-only, the read paths read-only, the write directory read-write, a
    fresh ``/proc``, ``/dev`` and scratch (``bwrap_scratch``), the links the interpreter
    is reached through (``Plan.links``) made again where no bind holds them, every
    namespace unshared (the network's included) and the root made read-only before the
    command starts."""
    scratch = bwrap_scratch(run)
    command = [binary, "--unshare-all", "--die-with-parent", "--new-session",
               "--cap-drop", "ALL", "--clearenv"]
    for name, value in environment(scratch, run.python, run.extra).items():
        command += ["--setenv", name, value]
    bound = []
    for path, target in system:
        if target is not None:
            command += ["--symlink", target, path]
        else:
            command += ["--ro-bind", path, path]
            bound.append(path)
    command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", scratch]
    for name in SCRATCH_DIRS:
        command += ["--dir", f"{scratch}/{name}"]
    for path in _outermost([*run.runtime, *run.reads], bound):
        command += ["--ro-bind", path, path]
    made = {path for path, _ in system}
    for path, target in run.links:  # those a bind above does not already hold
        if path not in made and _outermost([path], [*bound, *run.runtime, *run.reads,
                                                     run.write]):
            command += ["--symlink", target, path]
            made.add(path)
    command += ["--bind", run.write, run.write, "--dir", run.cwd, "--remount-ro", "/",
                "--chdir", run.cwd, "--", run.python, "-I", "-c", BOOTSTRAP, str(ready),
                run.python, *args]
    return command


# ------------------------------------------------------------ sandbox-exec
def _ancestors(path: str) -> list[str]:
    parts = Path(path).parents
    return [str(p) for p in parts]


def seatbelt(run: Plan, scratch: str) -> tuple[str, dict[str, str]]:
    """The Seatbelt profile for ``run`` and its parameters (``-D NAME=path``). The
    profile names paths only through ``(param ...)``, so its text is the same whatever
    the paths hold; ``scratch`` is the private temporary directory the caller made."""
    params: dict[str, str] = {}

    def param(prefix: str, paths: Sequence[str]) -> list[str]:
        names = []
        for i, path in enumerate(paths):
            name = f"{prefix}_{i}"
            params[name] = path
            names.append(f'(param "{name}")')
        return names

    runtime = param("RUNTIME", run.runtime)
    reads = param("READ", run.reads)
    [write] = param("WRITE", [run.write])
    [tmp] = param("SCRATCH", [scratch])
    allowed = [*run.runtime, *run.reads, run.write, scratch, *MAC_READ, *MAC_READ_FILES]
    links = [path for path, _ in run.links]  # a link is followed by reading its metadata
    metadata = param("ANCESTOR", sorted({a for p in (*allowed, *links) for a in _ancestors(p)}
                                        | set(links)))
    listed = param("LIST", run.lists)

    def rule(kind: str, items: Sequence[str]) -> str:
        return "\n  ".join([f"(allow {kind}", *items]) + ")"

    subpaths = [*(f'(subpath "{p}")' for p in MAC_READ), *(f"(subpath {p})" for p in runtime)]
    lines = [
        "(version 1)",
        "(deny default)",
        *(["(allow process-fork)"] if run.children else []),
        rule("process-exec", subpaths),
        "(allow signal (target same-sandbox))",
        "(allow process-info* (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow ipc-posix-sem)",
        rule("mach-lookup", [f'(global-name "{s}")' for s in MAC_SERVICES]),
        rule("file-read*", [*subpaths, *(f"(subpath {p})" for p in reads),
                            *(f'(literal "{p}")' for p in MAC_READ_FILES)]),
        rule("file-map-executable", subpaths),
        rule("file-read-metadata", ['(literal "/")', '(literal "/etc")', '(literal "/tmp")',
                                    '(literal "/var")', *(f"(literal {p})" for p in metadata)]),
        rule("file-read* file-write*", [f"(subpath {write})", f"(subpath {tmp})"]),
        *([rule("file-read-data", [f"(literal {p})" for p in listed])] if listed else []),
        rule("file-read* file-write-data", ['(literal "/dev/null")', '(subpath "/dev/fd")']),
    ]
    for value in params.values():
        if "\0" in value or "\n" in value:
            raise SandboxFailed(f"refused: a path with a control character: {value!r}")
    return "\n".join(lines) + "\n", params


def sandbox_exec_args(binary: str, run: Plan, args: Sequence[str], ready: int,
                      scratch: str) -> list[str]:
    profile, params = seatbelt(run, scratch)
    return [binary, "-p", profile, *(f"-D{name}={value}" for name, value in params.items()),
            "--", run.python, "-I", "-c", BOOTSTRAP, str(ready), run.python, *args]


# ------------------------------------------------------------------ running
@dataclass(frozen=True)
class Sandbox:
    """One backend (``kind``, one of ``KINDS``) and the program that runs it."""

    kind: str
    binary: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, not {self.kind!r}")

    def always_reads(self) -> tuple[str, ...]:
        """What every run can read, whatever paths its caller names: the interpreter's
        directories and the system's. Nothing to be kept from the code may lie under
        one of them."""
        _, runtime = interpreter()
        if self.kind == "bwrap":
            return (*runtime, *(path for path, _ in linux_system()))
        return (*runtime, *MAC_READ, *MAC_READ_FILES)

    @contextlib.contextmanager
    def _scratch(self, run: Plan) -> Iterator[str]:
        """The private temporary directory: bwrap mounts a fresh one (``bwrap_scratch``);
        for sandbox-exec it is made here (resolved) and removed after the run."""
        if self.kind == "bwrap":
            yield bwrap_scratch(run)
            return
        folder = _real(tempfile.mkdtemp(prefix="l2-sandbox-"))
        try:
            for name in SCRATCH_DIRS:
                os.mkdir(os.path.join(folder, name))
            yield folder
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def command(self, run: Plan, args: Sequence[str], ready: int, scratch: str) -> list[str]:
        if self.kind == "bwrap":
            return bwrap_args(self.binary, run, args, ready, linux_system())
        return sandbox_exec_args(self.binary, run, args, ready, scratch)

    def run(self, args: Sequence[str], *, cwd: Path, reads: Sequence[Path], write: Path,
            extra: Mapping[str, str] | None = None, timeout: float,
            merge: bool = False, lists: Sequence[Path] = (), children: bool = True) -> Ran:
        """Run the interpreter with ``args`` in the sandbox, in ``cwd``, reading
        ``reads`` and writing ``write``; ``extra`` adds variables to its environment.
        ``lists``: directories whose names (not their files) may be listed, as Python's
        import system needs for a directory on ``sys.path`` (under bwrap a listing shows
        only the mount points in it anyway).
        ``children=False`` keeps the code from starting processes where that is the only
        way to stop them all: sandbox-exec has no pid namespace, and a process that leaves
        the process group (``setsid``) would outlive the run. Under bwrap the run's pid
        namespace ends with it, so ``children`` changes nothing there.
        The run is stopped (its whole process group killed) after ``timeout`` seconds.
        ``merge`` sends stderr to stdout. ``SandboxFailed`` when it did not start."""
        run = plan(cwd, reads, write, extra, lists, children)
        scrub(Path(run.write))
        with self._scratch(run) as scratch:
            env = environment(scratch, run.python, run.extra)
            ready_r, ready_w = os.pipe()
            try:
                try:
                    process = subprocess.Popen(
                        self.command(run, args, ready_w, scratch),
                        cwd="/" if self.kind == "bwrap" else run.cwd, env=env,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                        encoding="utf-8", errors="replace", pass_fds=(ready_w,),
                        close_fds=True, start_new_session=True)
                except OSError as error:
                    raise SandboxFailed(f"{self.kind} could not be started ({error}), so "
                                        "nothing ran") from error
                finally:
                    os.close(ready_w)
                try:
                    return self._wait(process, ready_r, timeout)
                finally:
                    _kill(process)
            finally:
                os.close(ready_r)
                scrub(Path(run.write))

    def _wait(self, process: subprocess.Popen, ready: int, timeout: float) -> Ran:
        started = time.monotonic()
        readable, _, _ = select.select([ready], [], [], timeout)
        if not readable or os.read(ready, 1) != b"1":
            _kill(process)
            output, errors = process.communicate()
            said = "\n".join(p.strip() for p in (output, errors) if p and p.strip())
            raise SandboxFailed(f"the {self.kind} sandbox did not start, so nothing ran"
                                + (f": {said[-2000:]}" if said else
                                   f" (no output; exit code {process.returncode})"))
        left = max(0.0, timeout - (time.monotonic() - started))
        try:
            output, errors = process.communicate(timeout=left)
            return Ran(self._status(process.returncode), output or "", errors or "", False)
        except subprocess.TimeoutExpired:
            _kill(process)
            output, errors = process.communicate()
            return Ran(None, output or "", errors or "", True)

    def _status(self, code: int) -> int:
        """The code's exit code as ``Popen`` gives it outside (``-N`` for signal ``N``).
        bwrap exits ``128 + N`` when the code died of signal ``N``, so under bwrap a code
        above 128 is read as a signal (code that exits with such a number itself is then
        reported as that signal). sandbox-exec runs the code in its own process, so its
        code is the code's own."""
        return -(code - 128) if self.kind == "bwrap" and 128 < code < 128 + 65 else code


def _kill(process: subprocess.Popen) -> None:
    """Kill the run's whole process group, if any is left. Inside bwrap the code is in a
    pid namespace of its own, which ends with bwrap (``--die-with-parent``)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def scrub(directory: Path) -> list[str]:
    """Remove everything under ``directory`` that is not a regular file or a directory
    (links are never followed); the paths removed, relative to it. It fails closed: each
    directory is first made listable by its owner (the code may have made one
    unlistable, e.g. mode 0o111, to hide a link in it), and one that still cannot be
    listed raises ``OSError`` rather than being skipped. ``directory`` itself, when
    something else (a link) took its place, is removed and made again (``"."``)."""
    top = os.fspath(directory)
    removed = []
    if not stat.S_ISDIR(os.lstat(top).st_mode):
        os.unlink(top)
        os.mkdir(top)
        removed.append(".")
    folders = [top]
    while folders:
        folder = folders.pop()
        os.chmod(folder, stat.S_IMODE(os.lstat(folder).st_mode) | stat.S_IRWXU)
        with os.scandir(folder) as entries:
            for entry in entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    folders.append(entry.path)
                elif not stat.S_ISREG(mode):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(entry.path)
                    removed.append(os.path.relpath(entry.path, top))
    return sorted(removed)


def detect() -> Sandbox:
    """The backend for this platform; ``SandboxFailed`` when there is none here."""
    if sys.platform.startswith("linux"):
        binary = shutil.which("bwrap")
        if binary is None:
            raise SandboxFailed("bwrap (bubblewrap) is not installed: install it (e.g. "
                                "apt-get install bubblewrap) to run L2's code in a sandbox")
        return Sandbox("bwrap", binary)
    if sys.platform == "darwin":
        if not os.path.exists(SANDBOX_EXEC):
            raise SandboxFailed(f"{SANDBOX_EXEC} is missing")
        return Sandbox("sandbox-exec", SANDBOX_EXEC)
    raise SandboxFailed(f"no sandbox for {sys.platform}: L2's sandbox needs Linux (bwrap) "
                        "or macOS (sandbox-exec)")


# ------------------------------------------------------------------- check
PROBE = """
import json, os, socket, sys
spec = json.loads(sys.argv[1])

def attempt(do):
    try:
        return [True, do()]
    except Exception as error:
        return [False, f"{type(error).__name__}: {error}"]

def tcp():
    socket.create_connection(("127.0.0.1", spec["tcp"]), timeout=3).close()

def udp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(b"probe", ("127.0.0.1", spec["udp"]))

def outbound():
    socket.create_connection((spec["outbound"], 443), timeout=3).close()

def read(path):
    with open(path, "rb") as file:
        return len(file.read())

def write(path):
    with open(path, "x") as file:
        file.write("probe")

print(json.dumps({
    "tcp": attempt(tcp), "udp": attempt(udp), "outbound": attempt(outbound),
    "hidden": {p: attempt(lambda p=p: read(p)) for p in spec["hidden"]},
    "listed": {p: attempt(lambda p=p: sorted(os.listdir(p))) for p in spec["listed"]},
    "allowed": attempt(lambda: read(spec["allowed"])),
    "outside": {p: attempt(lambda p=p: write(p)) for p in spec["outside"]},
    "inside": attempt(lambda: write(spec["inside"])),
    "env": sorted(os.environ)}))
"""
SELF_SET = {"PWD", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
"""Variables the backend, the interpreter or the platform set inside, from their own
values (bwrap's ``--chdir`` sets ``PWD``)."""


@dataclass
class Check:
    """``check``'s result: the backend's kind (``None`` when there is none here) and, for
    each property of ``PROPERTIES``, whether it held and why."""

    kind: str | None
    properties: dict[str, tuple[bool, str]]

    @property
    def passed(self) -> bool:
        return set(self.properties) == set(PROPERTIES) and all(
            held for held, _ in self.properties.values())

    def failed(self) -> list[str]:
        return [f"{name}: {self.properties[name][1]}" if name in self.properties else
                f"{name}: not checked" for name in PROPERTIES
                if not self.properties.get(name, (False, ""))[0]]

    def as_json(self) -> dict:
        return {"kind": self.kind, "passed": self.passed,
                "properties": {name: {"held": held, "detail": detail}
                               for name, (held, detail) in self.properties.items()}}


def _check_base() -> Path:
    """Where the check lays out its audit: under the checkout's ``logs/``, beside where
    audits live (outside the system temporary directory, which bwrap replaces), else the
    home directory, else the temporary directory."""
    from experiments.l2.experiment import REPO

    for parent in (REPO / "logs", Path.home()):
        with contextlib.suppress(OSError):
            parent.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix=".l2-sandbox-check-", dir=parent))
    return Path(tempfile.mkdtemp(prefix="l2-sandbox-check-"))


def check(sandbox: Sandbox | None = None, base: Path | None = None) -> Check:
    """Run the probe in ``sandbox`` (``detect()`` by default) and judge each property.
    ``base``: an empty directory to lay the test audit out in (made and removed here by
    default)."""
    if sandbox is None:
        try:
            sandbox = detect()
        except SandboxFailed as error:
            return Check(None, {"starts": (False, str(error))})
    made = base is None
    base = _check_base() if base is None else Path(base)
    try:
        return _check(sandbox, Path(_real(base)))
    finally:
        if made:
            shutil.rmtree(base, ignore_errors=True)


def _check(sandbox: Sandbox, base: Path) -> Check:
    from experiments.l2.experiment import REPO

    tag = secrets.token_hex(6)
    audit = base / "audit"
    source, figures, l2 = audit / "experiments" / "exp-1", audit / "figures", audit / "l2"
    for folder in (source, figures, l2):
        folder.mkdir(parents=True, exist_ok=True)
    (source / "runs.jsonl").write_text("{}\n")
    hidden = [base / ".env", l2 / "log.eval"]
    for canary in hidden:
        canary.write_text(f"OPENROUTER_API_KEY=canary-{tag}\n")
    home = Path(_real(Path.home()))
    home_canary = home / f".l2-sandbox-canary-{tag}"
    with contextlib.suppress(OSError):
        home_canary.write_text(f"canary-{tag}\n")
        hidden.append(home_canary)
    hidden += [p for p in (REPO / ".env", REPO / "pyproject.toml") if p.exists()]
    listed = [base, l2, home, REPO]
    outside = [base / f"outside-{tag}", audit / f"outside-{tag}", source / f"outside-{tag}"]
    if os.access(home, os.W_OK):
        outside.append(home / f".l2-sandbox-write-{tag}")
    inside = figures / f"inside-{tag}"
    variable = f"L2_SANDBOX_CANARY_{tag.upper()}"
    found: dict[str, tuple[bool, str]] = {}
    with contextlib.ExitStack() as stack:
        listener = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.setblocking(False)
        datagrams = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        datagrams.bind(("127.0.0.1", 0))
        datagrams.setblocking(False)
        spec = {"tcp": listener.getsockname()[1], "udp": datagrams.getsockname()[1],
                "outbound": OUTBOUND, "hidden": [str(p) for p in hidden],
                "listed": [str(p) for p in listed], "allowed": str(source / "runs.jsonl"),
                "outside": [str(p) for p in outside], "inside": str(inside)}
        os.environ[variable] = tag
        try:
            ran = sandbox.run(["-I", "-c", PROBE, json.dumps(spec)], cwd=audit,
                              reads=[source], write=figures, timeout=CHECK_TIMEOUT)
        except SandboxFailed as error:
            return Check(sandbox.kind, {"starts": (False, str(error))})
        finally:
            os.environ.pop(variable, None)
            leftovers = [p for p in outside if p.exists()]
            for path in leftovers:
                path.unlink()
            home_canary.unlink(missing_ok=True)
        try:
            seen = json.loads(ran.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return Check(sandbox.kind, {"starts": (False, "the probe gave no result: "
                                                          + (ran.stderr or ran.stdout)[-2000:])})
        found["starts"] = (True, "the bootstrap byte arrived and the probe ran")
        try:
            listener.accept()
            accepted = True
        except BlockingIOError:
            accepted = False
        found["no-local-tcp"] = (not seen["tcp"][0] and not accepted,
                                 "connected to 127.0.0.1" if seen["tcp"][0] or accepted
                                 else seen["tcp"][1])
        try:
            datagrams.recvfrom(64)
            arrived = True
        except BlockingIOError:
            arrived = False
        found["no-udp"] = (not arrived, "a datagram arrived on 127.0.0.1" if arrived else
                           seen["udp"][1] if not seen["udp"][0] else "sent, never arrived")
    found["no-outbound"] = (not seen["outbound"][0], f"connected to {OUTBOUND}"
                            if seen["outbound"][0] else seen["outbound"][1])
    shown = [p for p, (ok, _) in seen["hidden"].items() if ok]
    allowed_names = _visible_names([source, figures, *interpreter()[1]])
    for path, (ok, names) in seen["listed"].items():
        extra = sorted(set(names) - allowed_names.get(path, set())) if ok else []
        if extra:
            shown.append(f"{path} lists {extra}")
    found["hidden-files"] = (not shown, f"visible: {shown}" if shown else
                             f"{len(seen['hidden'])} files unreadable, "
                             f"{len(seen['listed'])} directories hidden")
    found["reads-sources"] = (seen["allowed"][0], "read the source" if seen["allowed"][0]
                              else seen["allowed"][1])
    wrote = [p for p, (ok, _) in seen["outside"].items() if ok]
    wrote += [str(p) for p in leftovers if str(p) not in wrote]
    found["writes-outside-fail"] = (not wrote, f"written: {wrote}" if wrote else
                                    f"{len(outside)} writes refused")
    kept = inside.is_file() and inside.read_text() == "probe"
    found["writes-inside-work"] = (seen["inside"][0] and kept,
                                   "written and seen outside" if seen["inside"][0] and kept
                                   else str(seen["inside"][1]))
    leaked = sorted(name for name in seen["env"] if name == variable or (
        name in os.environ and name not in environment("/tmp", "/") and name not in SELF_SET))
    found["clean-environment"] = (not leaked, f"visible: {leaked}" if leaked else
                                  "no variable of the caller")
    return Check(sandbox.kind, found)


def _visible_names(allowed: Sequence[Path | str]) -> dict[str, set[str]]:
    """For each directory above an allowed path, the name of the next step towards it:
    what a listing of it may show (bwrap makes those steps as mount points)."""
    names: dict[str, set[str]] = {}
    for path in allowed:
        path = Path(_real(path))
        for parent, child in zip(path.parents, [path, *path.parents], strict=False):
            names.setdefault(str(parent), set()).add(child.name)
    return names


def main(argv: Sequence[str] | None = None) -> None:
    """``python -m experiments.l2.sandbox --check [--json]``."""
    parser = argparse.ArgumentParser(
        description="Check that L2's sandbox starts and holds on this machine.")
    parser.add_argument("--check", action="store_true", required=True,
                        help="run the self-test: exit 0 only when every property holds")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    result = check()
    if args.json:
        print(json.dumps(result.as_json(), sort_keys=True))
    elif result.passed:
        print(f"sandbox ok: {result.kind}")
    else:
        for line in result.failed():
            print(f"failed: {line}")
    raise SystemExit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
