"""L2's sandbox, the pure parts (``experiments.l2.sandbox``; spec
``docs/level2-interface.md`` §6, §12, contract test L2-26): the ``bwrap`` command line
and the Seatbelt profile for given paths, the environment, the cleaning of the write
directory and the check's verdict. They run on every platform; ``tests/e2e/test_l2_sandbox.py`` runs the
real thing."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from experiments.l2 import sandbox as sandbox_
from experiments.l2.sandbox import (
    BOOTSTRAP,
    PROPERTIES,
    Check,
    Plan,
    Sandbox,
    SandboxFailed,
    bwrap_args,
    environment,
    sandbox_exec_args,
    scrub,
    seatbelt,
)

HOME = "/home/ada"
CHECKOUT = f"{HOME}/ma-auto-evals"
AUDIT = f"{CHECKOUT}/logs/l2-audit/a1"
RUN = Plan(python=f"{CHECKOUT}/.venv/bin/python",
           runtime=(f"{HOME}/.local/share/uv/python/cpython-3.13", f"{CHECKOUT}/.venv"),
           reads=(f"{AUDIT}/experiments/exp-1", f"{AUDIT}/experiments/exp-2"),
           write=f"{AUDIT}/figures", cwd=AUDIT, extra={"L2_SOURCES": "x"})
SYSTEM = [("/usr", None), ("/bin", "usr/bin"), ("/lib", "usr/lib"),
          ("/etc/ld.so.cache", None)]


def bound(command: list[str]) -> dict[str, list[str]]:
    """Each bind option of a bwrap command line, with the sources it binds."""
    found: dict[str, list[str]] = {}
    for i, arg in enumerate(command):
        if arg in ("--ro-bind", "--bind", "--ro-bind-try", "--bind-try", "--dev-bind"):
            found.setdefault(arg, []).append(command[i + 1])
    return found


def test_bwrap_binds_the_runtime_and_reads_read_only_and_writes_one_directory():
    command = bwrap_args("/usr/bin/bwrap", RUN, ["-I", "-c", "print(1)"], 7, SYSTEM)
    binds = bound(command)
    assert binds["--bind"] == [RUN.write]
    assert set(binds["--ro-bind"]) == {"/usr", "/etc/ld.so.cache", *RUN.runtime, *RUN.reads}
    assert ["--symlink", "usr/bin", "/bin"] == command[command.index("/bin") - 2:
                                                       command.index("/bin") + 1]
    for path in (HOME, CHECKOUT, AUDIT, f"{AUDIT}/l2", f"{CHECKOUT}/.env", "/"):
        assert path not in binds["--ro-bind"] + binds["--bind"]
    assert "--dev-bind" not in binds
    # the write directory comes after the read binds, the root is made read-only last
    assert command.index("--bind") > max(i for i, a in enumerate(command) if a == "--ro-bind")
    assert command.index("--remount-ro") > command.index("--bind")
    assert command[command.index("--chdir") + 1] == AUDIT


def test_bwrap_has_no_network_and_an_environment_from_nothing():
    command = bwrap_args("bwrap", RUN, ["-c", "x"], 7, SYSTEM)
    assert "--unshare-all" in command and "--share-net" not in command
    assert {"--die-with-parent", "--new-session", "--clearenv"} <= set(command)
    names = [command[i + 1] for i, a in enumerate(command) if a == "--setenv"]
    assert set(names) == set(environment("/tmp", RUN.python, RUN.extra))
    assert ["--tmpfs", "/tmp"] == command[command.index("--tmpfs"):command.index("--tmpfs") + 2]


def test_bwrap_moves_the_scratch_off_tmp_when_a_path_lies_under_it():
    assert sandbox_.bwrap_scratch(RUN) == "/tmp"
    run = Plan(python=RUN.python, runtime=RUN.runtime, reads=("/tmp/pytest/audit/exp-1",),
               write="/tmp/pytest/audit/figures", cwd="/tmp/pytest/audit")
    command = bwrap_args("bwrap", run, [], 3, SYSTEM)
    scratch = sandbox_.ALT_SCRATCH
    assert command[command.index("--tmpfs") + 1] == scratch and "/tmp" not in command
    assert command[command.index("TMPDIR") + 1] == scratch


def test_bwrap_runs_the_bootstrap_first_with_the_ready_descriptor():
    command = bwrap_args("bwrap", RUN, ["-I", "-c", "print(1)"], 7, SYSTEM)
    tail = command[command.index("--") + 1:]
    assert tail == [RUN.python, "-I", "-c", BOOTSTRAP, "7", RUN.python, "-I", "-c", "print(1)"]


def test_bwrap_keeps_odd_paths_as_single_arguments():
    odd = f'{AUDIT}/ex"p\\1 $(touch x)/--share-net'
    run = Plan(python=RUN.python, runtime=RUN.runtime, reads=(odd,), write=RUN.write,
               cwd=AUDIT)
    command = bwrap_args("bwrap", run, [], 3, SYSTEM)
    assert bound(command)["--ro-bind"].count(odd) == 1
    assert "--share-net" not in command


def test_bwrap_does_not_bind_twice_what_a_system_path_holds():
    run = Plan(python="/usr/local/bin/python3", runtime=("/usr/local", f"{CHECKOUT}/.venv"),
               reads=(), write=RUN.write, cwd=AUDIT)
    binds = bound(bwrap_args("bwrap", run, [], 3, SYSTEM))["--ro-bind"]
    assert "/usr/local" not in binds and f"{CHECKOUT}/.venv" in binds


def test_the_seatbelt_profile_denies_by_default_and_has_no_network_rule():
    profile, params = seatbelt(RUN, "/private/var/folders/x/l2-sandbox-1")
    assert profile.startswith("(version 1)\n(deny default)\n")
    assert "network" not in profile and "(allow default" not in profile
    assert "file-write*" in profile
    [write_rule] = [line for line in profile.split("\n(allow") if "file-write*" in line]
    assert '(param "WRITE_0")' in write_rule and '(param "SCRATCH_0")' in write_rule
    assert "READ_" not in write_rule and "RUNTIME_" not in write_rule
    assert params["WRITE_0"] == RUN.write
    assert [params[f"READ_{i}"] for i in range(2)] == list(RUN.reads)


def test_the_seatbelt_profile_never_holds_a_path_of_the_home_or_the_checkout():
    profile, params = seatbelt(RUN, "/private/var/folders/x/l2-sandbox-1")
    assert HOME not in profile and CHECKOUT not in profile
    readable = [v for k, v in params.items() if not k.startswith(("ANCESTOR", "CWD"))]
    assert HOME not in readable and CHECKOUT not in readable and AUDIT not in readable
    # the home directory and the checkout appear only as ancestors: metadata, no listing
    ancestors = {v for k, v in params.items() if k.startswith("ANCESTOR")}
    assert {HOME, CHECKOUT, AUDIT} <= ancestors
    [metadata] = [line for line in profile.split("\n(allow") if "file-read-metadata" in line]
    assert "ANCESTOR" in metadata
    assert all("ANCESTOR" not in line for line in profile.split("\n(allow")
               if "file-read*" in line or "file-write" in line)


def test_a_path_with_a_quote_or_backslash_cannot_change_the_profile():
    odd = f'{AUDIT}/a") (allow network*) (subpath "/'
    run = Plan(python=RUN.python, runtime=RUN.runtime, reads=(odd, f"{AUDIT}/b\\"),
               write=RUN.write, cwd=AUDIT)
    profile, params = seatbelt(run, "/private/tmp/s")
    assert odd not in profile and "network" not in profile and "\\" not in profile
    assert params["READ_0"] == odd and params["READ_1"] == f"{AUDIT}/b\\"
    command = sandbox_exec_args("/usr/bin/sandbox-exec", run, ["-c", "x"], 5, "/private/tmp/s")
    assert f"-DREAD_0={odd}" in command
    assert command[command.index("--") + 1:] == [RUN.python, "-I", "-c", BOOTSTRAP, "5",
                                                 RUN.python, "-c", "x"]


def test_the_profile_text_does_not_depend_on_the_paths():
    other = Plan(python="/opt/py/bin/python", runtime=("/opt/py", "/srv/venv"),
                 reads=("/srv/a", "/srv/b"), write="/srv/w", cwd="/srv")
    mine, _ = seatbelt(RUN, "/private/tmp/s")
    theirs, _ = seatbelt(other, "/private/tmp/t")
    strip = [line for line in mine.splitlines() if "ANCESTOR" not in line]
    assert strip == [line for line in theirs.splitlines() if "ANCESTOR" not in line]


def test_only_the_names_of_a_listed_directory_are_readable():
    run = Plan(python=RUN.python, runtime=RUN.runtime, reads=RUN.reads, write=RUN.write,
               cwd=CHECKOUT, lists=(CHECKOUT,))
    profile, params = seatbelt(run, "/private/tmp/s")
    assert params["LIST_0"] == CHECKOUT
    [listing] = [line for line in profile.split("\n(allow") if "LIST_0" in line]
    assert listing.startswith(" file-read-data") and "(literal (param \"LIST_0\"))" in listing
    assert "LIST" not in seatbelt(RUN, "/private/tmp/s")[0]


def test_a_path_with_a_newline_is_refused():
    run = Plan(python=RUN.python, runtime=RUN.runtime, reads=(f"{AUDIT}/a\nb",),
               write=RUN.write, cwd=AUDIT)
    with pytest.raises(SandboxFailed, match="control character"):
        seatbelt(run, "/private/tmp/s")


def test_the_environment_holds_nothing_of_the_caller(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("COSMOS_TOKEN", "fake-token")
    env = environment("/tmp", RUN.python, {"L2_SOURCES": "/a:/b"})
    assert set(env) == {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "MPLBACKEND",
                        "MPLCONFIGDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                        "PYTHONDONTWRITEBYTECODE", "L2_SOURCES"}
    assert env["HOME"].startswith("/tmp/") and env["MPLCONFIGDIR"].startswith("/tmp/")
    assert env["MPLBACKEND"] == "Agg" and "sk-or-test" not in env.values()


def test_a_read_or_write_path_that_would_show_the_home_directory_is_refused(tmp_path):
    for broad in ("/", str(Path.home()), str(Path.home().parent)):
        with pytest.raises(SandboxFailed, match="home directory or the root"):
            sandbox_.plan(tmp_path, [Path(broad)], tmp_path)
    with pytest.raises(SandboxFailed, match="does not exist"):
        sandbox_.plan(tmp_path, [], tmp_path / "missing")


def test_paths_are_resolved(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    run = sandbox_.plan(tmp_path / "link", [tmp_path / "link"], tmp_path / "link")
    real = os.path.realpath(tmp_path / "real")
    assert run.reads == (real,) and run.write == real and run.cwd == real
    assert all(os.path.realpath(p) == p for p in run.runtime)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
def test_scrub_removes_links_fifos_and_sockets_and_follows_none(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside")
    write = tmp_path / "figures"
    (write / "sub").mkdir(parents=True)
    (write / "a.png").write_bytes(b"png")
    (write / "sub" / "b.png").write_bytes(b"png")
    (write / "link.png").symlink_to(outside / "keep.txt")
    (write / "sub" / "dir").symlink_to(outside, target_is_directory=True)
    os.mkfifo(write / "fifo")
    removed = scrub(write)
    assert sorted(removed) == ["fifo", "link.png", os.path.join("sub", "dir")]
    assert sorted(p.relative_to(write).as_posix() for p in write.rglob("*")) == [
        "a.png", "sub", "sub/b.png"]
    assert (outside / "keep.txt").read_text() == "outside"


def test_scrub_lists_a_directory_the_code_made_unlistable(tmp_path, monkeypatch):
    """A directory of mode 0o111 cannot be listed by its owner (root ignores modes, so
    ``scandir`` is made to keep them here): scrub makes it listable again and removes
    the link hidden in it, at any depth and in the write directory itself."""
    listing = os.scandir

    def keeps_modes(path):
        if not os.stat(path).st_mode & 0o400:
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return listing(path)

    monkeypatch.setattr(sandbox_.os, "scandir", keeps_modes)
    write = tmp_path / "figures"
    (write / "d" / "e").mkdir(parents=True)
    (write / "d" / "leak.png").symlink_to(tmp_path / "l2.jsonl")
    (write / "d" / "e" / "deeper.png").symlink_to(tmp_path / "l2.jsonl")
    for folder in (write / "d" / "e", write / "d", write):
        folder.chmod(0o111)
    assert scrub(write) == [os.path.join("d", "e", "deeper.png"), os.path.join("d", "leak.png")]
    assert not os.path.lexists(write / "d" / "leak.png")
    assert (write / "d").stat().st_mode & 0o700 == 0o700


def test_scrub_fails_on_a_directory_it_still_cannot_list(tmp_path, monkeypatch):
    def refused(path):
        raise PermissionError(13, "Permission denied", os.fspath(path))

    monkeypatch.setattr(sandbox_.os, "scandir", refused)
    (tmp_path / "figures").mkdir()
    with pytest.raises(PermissionError):
        scrub(tmp_path / "figures")


def test_scrub_puts_the_write_directory_back_when_a_link_took_its_place(tmp_path):
    (tmp_path / "l2").mkdir()
    (tmp_path / "figures").symlink_to(tmp_path / "l2", target_is_directory=True)
    assert scrub(tmp_path / "figures") == ["."]
    assert (tmp_path / "figures").is_dir() and not (tmp_path / "figures").is_symlink()
    assert (tmp_path / "l2").is_dir()


def test_bwrap_reports_a_signal_as_popen_does_outside():
    box = Sandbox("bwrap", "bwrap")
    assert [box._status(code) for code in (0, 3, 128, 137, 139, 192)] == [
        0, 3, 128, -9, -11, -64]
    assert Sandbox("sandbox-exec", "x")._status(137) == 137


def test_the_seatbelt_profile_forbids_processes_only_when_asked():
    assert "(allow process-fork)" in seatbelt(RUN, "/private/tmp/s")[0]
    kept = Plan(**{**RUN.__dict__, "children": False})
    profile = seatbelt(kept, "/private/tmp/s")[0]
    assert "process-fork" not in profile and "(deny default)" in profile


def test_detect_offers_nothing_on_another_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SandboxFailed, match="no sandbox for win32"):
        sandbox_.detect()


def test_detect_needs_bwrap_on_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox_.shutil, "which", lambda name: None)
    with pytest.raises(SandboxFailed, match="bubblewrap"):
        sandbox_.detect()
    monkeypatch.setattr(sandbox_.shutil, "which", lambda name: "/usr/bin/bwrap")
    assert sandbox_.detect() == Sandbox("bwrap", "/usr/bin/bwrap")


def test_a_check_passes_only_when_every_property_held():
    held = {name: (True, "fine") for name in PROPERTIES}
    assert Check("bwrap", held).passed and Check("bwrap", held).failed() == []
    one = {**held, "no-udp": (False, "a datagram arrived on 127.0.0.1")}
    assert not Check("bwrap", one).passed
    assert Check("bwrap", one).failed() == ["no-udp: a datagram arrived on 127.0.0.1"]
    started_only = Check(None, {"starts": (False, "no sandbox for win32")})
    assert not started_only.passed
    assert started_only.failed()[0] == "starts: no sandbox for win32"
    assert len(started_only.failed()) == len(PROPERTIES)


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="kind must be one of"):
        Sandbox("docker", "/usr/bin/docker")


def test_a_sandbox_names_what_every_run_can_read(monkeypatch):
    monkeypatch.setattr(sandbox_, "linux_system", lambda: [("/usr", None), ("/bin", "usr/bin")])
    runtime = sandbox_.interpreter()[1]
    assert Sandbox("bwrap", "/usr/bin/bwrap").always_reads() == (*runtime, "/usr", "/bin")
    mac = Sandbox("sandbox-exec", sandbox_.SANDBOX_EXEC).always_reads()
    assert mac == (*runtime, *sandbox_.MAC_READ, *sandbox_.MAC_READ_FILES)


def test_every_link_crossed_on_the_way_to_a_path_is_found(tmp_path):
    (tmp_path / "real" / "bin").mkdir(parents=True)
    (tmp_path / "real" / "bin" / "python3.13").write_text("")
    os.symlink("python3.13", tmp_path / "real" / "bin" / "python3")
    os.symlink(tmp_path / "real", tmp_path / "minor")  # as uv's cpython-3.13 link
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    os.symlink(tmp_path / "minor" / "bin" / "python3", tmp_path / "venv" / "bin" / "python")
    found = sandbox_.crossed(tmp_path / "venv" / "bin" / "python")
    base = os.path.realpath(tmp_path)
    assert [(os.path.realpath(os.path.dirname(a)) + "/" + os.path.basename(a), b)
            for a, b in found[-3:]] == [
        (f"{base}/venv/bin/python", str(tmp_path / "minor" / "bin" / "python3")),
        (f"{base}/minor", str(tmp_path / "real")),
        (f"{base}/real/bin/python3", "python3.13")]
    os.symlink("loop", tmp_path / "loop")
    assert len(sandbox_.crossed(tmp_path / "loop")) == sandbox_.MAX_LINKS  # ends


def test_bwrap_makes_again_the_links_to_the_interpreter_that_no_bind_holds():
    uv = f"{HOME}/.local/share/uv/python"
    links = ((f"{CHECKOUT}/.venv/bin/python", f"{uv}/cpython-3.13/bin/python3.13"),
             (f"{uv}/cpython-3.13", "cpython-3.13.9"), ("/bin", "usr/bin"))
    run = Plan(python=RUN.python, runtime=(f"{uv}/cpython-3.13.9", f"{CHECKOUT}/.venv"),
               reads=RUN.reads, write=RUN.write, cwd=AUDIT, links=links)
    command = bwrap_args("bwrap", run, [], 3, SYSTEM)
    made = [(command[i + 2], command[i + 1]) for i, a in enumerate(command) if a == "--symlink"]
    # the one inside the virtualenv's bind is there already; /bin is made once
    assert made == [("/bin", "usr/bin"), ("/lib", "usr/lib"),
                    (f"{uv}/cpython-3.13", "cpython-3.13.9")]
    assert command.index(f"{uv}/cpython-3.13") < command.index("--remount-ro")
    assert f"{uv}/cpython-3.13" not in bound(command).get("--ro-bind", [])


def test_the_seatbelt_profile_lets_a_link_to_the_interpreter_be_followed_not_read():
    link = f"{HOME}/.local/share/uv/python/cpython-3.13-link"
    run = Plan(python=RUN.python, runtime=RUN.runtime, reads=RUN.reads, write=RUN.write,
               cwd=AUDIT, links=((link, "cpython-3.13"),))
    profile, params = seatbelt(run, "/private/tmp/s")
    [name] = [k for k, v in params.items() if v == link]
    assert name.startswith("ANCESTOR")  # metadata only
    assert link not in profile


def test_the_seatbelt_profile_lists_the_root_and_the_working_directory_and_reads_no_file():
    """macOS 26's loader aborts before the program starts unless it may open ``/``, and
    ``getcwd`` fails unless it may open the working directory: their names, no file in
    them (contract test L2-26)."""
    profile, params = seatbelt(RUN, "/private/tmp/s")
    [listing] = [block for block in profile.split("\n(allow ") if block.startswith(
        "file-read-data")]
    assert listing.splitlines() == ["file-read-data", '  (literal "/")',
                                    '  (literal (param "CWD_0")))']
    assert params["CWD_0"] == AUDIT and '(subpath "/")' not in profile
    assert [block.split()[0] for block in profile.split("\n(allow ") if "CWD_0" in block] == [
        "file-read-data"]
    assert AUDIT in {v for k, v in params.items() if k.startswith("ANCESTOR")}
