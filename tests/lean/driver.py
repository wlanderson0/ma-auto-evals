"""The Lean model as a subprocess, and the JSON it is fed.

``LeanDriver`` runs ``lean/Driver/Main.lean`` with ``lean --run`` against the built
model (``lean/.lake/build/lib/lean``) and exchanges one JSON object per line. Set
``LEAN`` to the ``lean`` binary; without it, or without a built model, the
differential tests are skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from orbit.communication import CommunicationChannel, CommunicationMessage

from orbit_audit.delivery import CommunicationWorld, DeliveryOverlay

ROOT = Path(__file__).resolve().parents[2]
LEAN_DIR = ROOT / "lean"
BUILD = LEAN_DIR / ".lake" / "build" / "lib" / "lean"


def lean_binary() -> str | None:
    """The ``lean`` binary, if the built model is present and newer than its sources."""
    found = os.environ.get("LEAN") or shutil.which("lean")
    if not found:
        return None
    sources = list((LEAN_DIR / "Scope" / "Spec").glob("*.lean"))
    built = [BUILD / "Scope" / "Spec" / (s.stem + ".olean") for s in sources]
    if not all(b.exists() for b in built):
        return None
    if max(s.stat().st_mtime for s in sources) > min(b.stat().st_mtime for b in built):
        raise RuntimeError("the Lean model is older than its sources: run lean/build.sh")
    return found


class LeanDriver:
    def __init__(self, lean: str) -> None:
        env = {**os.environ, "LEAN_PATH": str(BUILD)}
        self.stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.proc = subprocess.Popen(
            [lean, "--run", str(LEAN_DIR / "Driver" / "Main.lean")], cwd=LEAN_DIR, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True,
            encoding="utf-8",
        )

    def ask(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.ask_raw(json.dumps(request), request)

    def ask_raw(self, line: str, request: Any = None) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        reply_line = self.proc.stdout.readline()
        if not reply_line:
            self.stderr.seek(0)
            raise RuntimeError(f"the Lean driver exited: {self.stderr.read()[-2000:]}")
        reply = json.loads(reply_line)
        if "fatal" in reply:
            raise ValueError(f"Lean driver: {reply['fatal']} for {request or line}")
        return reply

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait(timeout=30)
        self.stderr.close()


# ------------------------------------------------------------------ the world
def message(m: dict[str, Any], sequence: int) -> CommunicationMessage:
    return CommunicationMessage(id=m["id"], channel_id=m["channel"], sender=m["sender"],
                                recipients=tuple(m["recipients"]), content=m["content"],
                                sequence=sequence)


def python_world(world: dict[str, Any]) -> CommunicationWorld:
    """The ``CommunicationWorld`` a JSON world describes (channels' readers and writers explicit)."""
    channels = {c["id"]: CommunicationChannel(
        id=c["id"], name=c["id"], kind="group", members=tuple(sorted({*c["readers"], *c["writers"]})),
        readers=tuple(c["readers"]), writers=tuple(c["writers"])) for c in world["channels"]}
    routes = world["routes"]
    return CommunicationWorld(
        messages={m["id"]: message(m, i) for i, m in enumerate(world["messages"])},
        channels=channels,
        permitted_edges=None if routes is None else frozenset(map(tuple, routes)),
    )


def python_overlay(world: dict[str, Any]) -> DeliveryOverlay:
    ov = world["overlay"]
    overlay = DeliveryOverlay()
    for (mid, recipient), content in reversed(ov["rewrites"]):  # Lean keeps newest first
        overlay.rewrites[(mid, recipient)] = content
    overlay.drops.update((mid, recipient) for mid, recipient in ov["drops"])
    offset = len(world["messages"])
    overlay.injections.extend(message(m, offset + i) for i, m in enumerate(ov["injections"]))
    overlay.delivered.update((agent, mid) for agent, mid in ov["delivered"])
    return overlay
