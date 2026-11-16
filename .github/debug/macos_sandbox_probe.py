"""Temporary: find what the Seatbelt profile lacks on this macOS (removed before review).

Runs one line of Python under the real profile, then under the profile with one more rule
at a time, and prints which additions let the interpreter start."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from experiments.l2 import sandbox as s

EXTRA = [
    "(allow file-read*)",
    "(allow file-read-metadata)",
    "(allow file-read-data)",
    "(allow file-read-xattr)",
    "(allow file-test-existence)",
    "(allow file-map-executable)",
    '(allow file-read-data (literal "/"))',
    '(allow file-read* (subpath "/private/preboot"))',
    '(allow file-read* (subpath "/private/var/db"))',
    '(allow file-read* (subpath "/private/var"))',
    '(allow file-read* (subpath "/private"))',
    '(allow file-read* (subpath "/dev"))',
    '(allow file-read* (subpath "/Volumes"))',
    '(allow file-read* (subpath "/.vol"))',
    '(allow file-read* (subpath "/Users"))',
    '(allow file-read* (subpath "/opt"))',
    '(allow file-read* (subpath "/Applications"))',
    '(allow file-read* (subpath "/cores"))',
    '(allow file-read* (subpath "/bin") (subpath "/sbin"))',
    "(allow file-ioctl)",
    "(allow file*)",
    "(allow system-fsctl)",
    "(allow system-fcntl)",
    "(allow system-info)",
    "(allow system-mac-syscall)",
    "(allow system*)",
    "(allow sysctl*)",
    "(allow syscall-unix)",
    "(allow syscall-mach)",
    "(allow syscall*)",
    "(allow mach-lookup)",
    "(allow mach*)",
    "(allow ipc*)",
    "(allow iokit-open)",
    "(allow iokit-get-properties)",
    "(allow iokit*)",
    "(allow process-info*)",
    "(allow process-codesigning*)",
    "(allow process*)",
    "(allow nvram*)",
    "(allow user-preference-read)",
    "(allow darwin-notification-post)",
    "(allow dynamic-code-generation)",
    "(allow signal)",
    "(allow network*)",
    '(import "dyld-support.sb")',
    '(import "system.sb")',
    '(import "bsd.sb")',
]
WHOLE = {
    "allow default": "(version 1)\n(allow default)\n",
    "deny default, allow file* process* sysctl* mach* ipc* iokit* system* signal":
        "(version 1)\n(deny default)\n(allow file*)\n(allow process*)\n(allow sysctl*)\n"
        "(allow mach*)\n(allow ipc*)\n(allow iokit*)\n(allow system*)\n(allow signal)\n",
    "deny default, allow file-read* process-exec":
        "(version 1)\n(deny default)\n(allow file-read*)\n(allow process-exec)\n",
    "deny default, allow file-read* process* sysctl-read":
        "(version 1)\n(deny default)\n(allow file-read*)\n(allow process*)\n"
        "(allow sysctl-read)\n",
    "deny default, allow everything but files, plus the profile's file rules": None,
}


def show(title: str, command: list[str]) -> None:
    print(f"\n===== {title}", flush=True)
    ran = subprocess.run(command, capture_output=True, text=True, errors="replace")
    print((ran.stdout + ran.stderr)[-6000:], flush=True)


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="probe-"))
    plan = s.plan(work, [], work)
    box = s.detect()
    print("python", plan.python, "\nruntime", plan.runtime, "\nlinks", plan.links,
          "\nexecutable", sys.executable, "\nprefix", sys.prefix, sys.base_prefix, flush=True)
    show("venv", ["ls", "-la", str(Path(plan.python).parent)])
    show("cryptexes", ["sh", "-c", "ls -la /System/Cryptexes /System/Volumes/Preboot/Cryptexes "
                       "/private/preboot /System/Library/dyld 2>&1 | head -60; mount"])
    show("dyld-support.sb", ["sh", "-c", "cat /System/Library/Sandbox/Profiles/dyld-support.sb;"
                             " ls /System/Library/Sandbox/Profiles | head -400 | tr '\\n' ' '"])
    code = ["-I", "-c", "print('started')"]
    with box._scratch(plan) as scratch:
        profile, params = s.seatbelt(plan, scratch)
        env = s.environment(scratch, plan.python, plan.extra)
        defs = [f"-D{name}={value}" for name, value in params.items()]
        print("\n===== the profile\n" + profile, flush=True)

        def run(text: str, binary: list[str] | None = None) -> str:
            ran = subprocess.run([s.SANDBOX_EXEC, "-p", text, *defs, "--",
                                  *(binary or [plan.python, *code])], cwd=plan.cwd, env=env,
                                 capture_output=True, text=True, errors="replace", timeout=120)
            said = (ran.stdout + ran.stderr).strip().replace("\n", " | ")[:300]
            return f"exit {ran.returncode:>4}  {said}"

        outside = subprocess.run([plan.python, *code], cwd=plan.cwd, env=env,
                                 capture_output=True, text=True)
        print("outside, same environment:", outside.returncode, outside.stdout.strip(),
              outside.stderr.strip()[:300], flush=True)
        print("the profile as it is:      ", run(profile), flush=True)
        time.sleep(3)
        show("log: what the kernel said of that run", ["sh", "-c", (
            "sudo log show --last 30s --style compact --predicate "
            "'eventMessage CONTAINS \"Sandbox\" OR eventMessage CONTAINS[c] \"ignition\" OR "
            "eventMessage CONTAINS[c] \"python\"' 2>/dev/null | sed -E 's/^[^ ]+ [^ ]+ +//' "
            "| sort | uniq -c | sort -rn | head -n 80")])
        print("/usr/bin/true, the profile:", run(profile, ["/usr/bin/true"]), flush=True)
        print("/bin/echo, the profile:    ", run(profile, ["/bin/echo", "echo-ran"]), flush=True)
        print("\n===== the profile plus one rule", flush=True)
        for extra in EXTRA:
            print(f"{extra:<60} {run(profile + extra + chr(10))}", flush=True)
        print("\n===== whole profiles", flush=True)
        WHOLE["deny default, allow everything but files, plus the profile's file rules"] = (
            "(version 1)\n(deny default)\n(allow process*)\n(allow sysctl*)\n(allow mach*)\n"
            "(allow ipc*)\n(allow iokit*)\n(allow system*)\n(allow signal)\n"
            + "".join(f"(allow {block}\n" for block in profile.split("\n(allow ")[1:]
                      if block.startswith("file")))
        for name, text in WHOLE.items():
            print(f"{name:<75} {run(text)}", flush=True)
    print(os.uname(), flush=True)


if __name__ == "__main__":
    main()
