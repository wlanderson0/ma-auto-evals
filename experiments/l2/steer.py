"""Steering a running L2 audit through a mailbox directory (Cosmos spec 22, §5, §5.1, §6;
``docs/level2-interface.md`` §7.1).

The mailbox is a directory, ``COSMOS_STEER_DIR`` when it is set, else ``.cosmos-steer`` in
the Inspect log directory of L2's own eval (``<audit>/l2/.cosmos-steer``). It holds three
files, each line one JSON object with ``"schema": "cosmos.steer/1"``:

- ``program.json``: written by the audit when it first looks at the mailbox: its process,
  when it began to read, the kinds it takes, its sandbox (``Audit.sandbox_report``) and the
  highest budget a ``budget`` request may set (``COSMOS_STEER_MAX_BUDGET_USD``).
- ``inbox.jsonl``: one line per request (``pause``, ``resume``, ``stop``, ``budget``,
  ``answer``, ``note``), written by a Cosmos plugin (``"via": "cosmos"``) or by this
  module's command (``"via": "local"``).
- ``outbox.jsonl``: written by the audit: an ``ack`` for every request it read, applied or
  refused with a reason, and L2's questions (``ask``, ``ask_closed``) and replies (``say``).

Steering is off unless ``COSMOS_STEER=1`` or ``COSMOS_STEER_DIR`` is set, or the audit
command gets ``--steer``. ``Steering`` is the audit's side; ``main`` is the command::

    python -m experiments.l2.steer [--dir PATH] note "<text>" | pause | resume | stop
        | budget <usd> | answer <ask_key> <choice> [--text "..."] | status [--json]

Every reader and writer refuses a directory or file that is a symbolic link, is not the
user's own, or is open to others (the directory 0700, the files 0600). A line is appended
with one ``write`` under an exclusive ``flock`` on the file; readers take no lock and read
only up to the last newline. POSIX only: without ``fcntl`` steering is off.

This module never imports cosmos: the files are the whole interface.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import getpass
import json
import math
import os
import re
import stat
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows: no flock, no POSIX modes
    fcntl = None  # type: ignore[assignment]

SCHEMA = "cosmos.steer/1"
PROGRAM = "ma-auto-evals/l2"
"""The program's name in ``program.json``."""
DIR_NAME = ".cosmos-steer"
"""The mailbox's name in L2's Inspect log directory, when ``COSMOS_STEER_DIR`` is not set."""
PROGRAM_FILE, INBOX, OUTBOX = "program.json", "inbox.jsonl", "outbox.jsonl"
KINDS = ("pause", "resume", "stop", "budget", "answer", "note")
"""Every request kind (Cosmos spec 22 §3), in the order ``program.json`` lists them."""
REASONS = ("expired", "before start", "malformed", "not implemented", "no sandbox", "closed",
           "not offered", "not a raise", "over the maximum", "ended")
"""Why a request is refused (spec 22 §5; ``ended``: the audit ended or is concluding, so
the line can no longer be applied)."""
MAX_LINE = 16 * 1024
"""Bytes: a longer line is malformed."""
MAX_TEXT = 2000
"""Characters in a note or an answer's text."""
MAX_SAY = 4000
"""Characters in a reply of L2's (``say``)."""
MAX_CHOICES = 4
LIFETIME = timedelta(minutes=15)
"""How long a line the command writes may wait to be read."""
SKEW = timedelta(minutes=2)
"""How far a line's ``sent_at`` may be ahead of the program's clock, and its lifetime over
``LIFETIME`` (cosmos refuses a ``created_at`` more than two minutes from its own clock)."""
ASK_TIMEOUT = 300.0
"""Seconds a question waits for an answer when L2 sets no timeout."""
ASK_MAX = 1800.0
"""The longest a question may wait."""
POLL = 1.0
"""Seconds between looks at the mailbox while the audit waits (a pause, a question)."""
NO_SANDBOX = ("this audit runs model-written code without a sandbox: send only words you "
              "wrote yourself")
"""The command's fixed line before it writes text to an audit without a sandbox."""
OFF_HERE = ("steering is off: the mailbox needs POSIX file modes and flock, which this "
            "system does not have")
EVENT = "l2_steer"
EVENT_VERSION = 1

_FIELDS = {"cosmos": {"schema", "id", "seq", "kind", "payload", "via", "author", "sent_at",
                      "expires_at"},
           "local": {"schema", "id", "kind", "payload", "via", "author", "sent_at",
                     "expires_at"}}
_AUTHOR = {"cosmos": {"login", "key"}, "local": {"local"}}
_PAYLOAD = {"pause": set(), "resume": set(), "stop": set(), "budget": {"usd"},
            "answer": {"ask_key", "choice", "text"}, "note": {"text", "hypothesis"}}
# ASCII digits only: ``\d`` alone also takes every other script's digits
_AMOUNT = re.compile(r"\d{1,12}(\.\d{1,6})?", re.ASCII)
_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,9})?(Z|\+00:00)", re.ASCII)
_KEY = re.compile(r"(ask|say)-(\d+)", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
"""A GitHub login, the only shape a cosmos author's ``login`` takes (it reaches L2, R18)."""


class SteerError(Exception):
    """The mailbox cannot be used, or a request is refused before it is written; the
    message is one line that says why."""


class Refusal(Exception):
    """A steering tool call (``ask_researcher``, ``reply_researcher``) that cannot be done
    as asked; the message goes back to L2."""


# ----------------------------------------------------------------- settings
def requested(flag: bool = False, environ: Mapping[str, str] = os.environ) -> bool:
    """Whether steering is asked for: ``--steer``, ``COSMOS_STEER=1`` or
    ``COSMOS_STEER_DIR``."""
    return flag or environ.get("COSMOS_STEER") == "1" or bool(environ.get("COSMOS_STEER_DIR"))


def sandbox_required(environ: Mapping[str, str] = os.environ) -> bool:
    """``COSMOS_REQUIRE_SANDBOX=1``: the audit starts only if it will report a sandbox."""
    return environ.get("COSMOS_REQUIRE_SANDBOX") == "1"


def directory(audit_out: Path, environ: Mapping[str, str] = os.environ) -> Path:
    """The mailbox: ``COSMOS_STEER_DIR``, else ``<audit>/l2/.cosmos-steer``."""
    given = environ.get("COSMOS_STEER_DIR")
    return Path(given) if given else Path(audit_out) / "l2" / DIR_NAME


def under(mailbox: Path, visible: Sequence[Path]) -> Path | None:
    """The first of ``visible`` (paths a sandbox can read or write) that holds ``mailbox``,
    links resolved; ``None`` when the mailbox is outside them all, as it must be: code in
    the sandbox must not be able to read or write a line (Cosmos spec 22 §5)."""
    place = Path(os.path.realpath(mailbox))
    for path in visible:
        root = Path(os.path.realpath(path))
        if place == root or root in place.parents:
            return Path(path)
    return None


def amount(value: Any) -> Decimal | None:
    """A plain decimal string (``"15.00"``) as a ``Decimal``; ``None`` for anything else (a
    number, a sign, an exponent, ``NaN``)."""
    if not isinstance(value, str) or not _AMOUNT.fullmatch(value):
        return None
    return Decimal(value)


def max_budget(environ: Mapping[str, str] = os.environ) -> Decimal | None:
    """``COSMOS_STEER_MAX_BUDGET_USD``, the highest total a ``budget`` request may set;
    ``SteerError`` for a value that is not a plain decimal string."""
    given = environ.get("COSMOS_STEER_MAX_BUDGET_USD")
    if given is None or given == "":
        return None
    found = amount(given)
    if found is None or found <= 0:
        raise SteerError(f"COSMOS_STEER_MAX_BUDGET_USD must be a plain decimal amount in "
                         f"dollars, such as 25.00, not {given!r}")
    return found


def usd(value: Decimal) -> str:
    """A decimal string with at least two places, as ``program.json`` and the record write
    amounts."""
    return f"{value:.2f}" if value == value.quantize(Decimal("0.01")) else format(value, "f")


# -------------------------------------------------------------------- times
def now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    """RFC 3339 in UTC, to the millisecond."""
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{moment.microsecond // 1000:03d}Z"


def parse_time(value: Any) -> datetime | None:
    """An RFC 3339 time in UTC (``Z`` or ``+00:00``); ``None`` for anything else."""
    found = _TIME.fullmatch(value) if isinstance(value, str) else None
    if found is None:
        return None
    try:
        moment = datetime.strptime(found[1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    fraction = (found[2] or ".")[1:7]
    return moment.replace(microsecond=int(fraction.ljust(6, "0")) if fraction else 0)


# --------------------------------------------------------------------- text
def clean(text: str, newlines: bool = True) -> bool:
    """No control character (but a newline or a tab, when ``newlines``) and no lone
    surrogate."""
    allowed = "\n\t" if newlines else ""
    return not any((unicodedata.category(c) in ("Cc", "Cs")) and c not in allowed
                   for c in text)


def escape(text: Any, newlines: bool = False) -> str:
    """``text`` safe to print on a terminal: every unprintable character (an escape
    sequence's ESC included) as a Python escape; newlines kept when ``newlines``."""
    out = []
    for c in str(text):
        if c.isprintable() or (newlines and c == "\n"):
            out.append(c)
        else:
            code = ord(c)
            out.append(f"\\x{code:02x}" if code < 0x100 else
                       f"\\u{code:04x}" if code < 0x10000 else f"\\U{code:08x}")
    return "".join(out)


def who(author: Mapping[str, Any]) -> str:
    """An inbox line's author, for people: a login, or the user on the machine."""
    if "local" in author:
        return f"{author['local']} (sent on the machine)"
    return str(author.get("login"))


def show(line: str) -> None:
    """A line in the owner's terminal: the process's own stderr, which Inspect's display
    does not hold, whatever the progress view (``--quiet`` included)."""
    print(line, file=sys.__stderr__ or sys.stderr, flush=True)


# ----------------------------------------------------------------- requests
@dataclass(frozen=True)
class Request:
    """An inbox line that fits its shape."""

    id: str
    kind: str
    payload: dict[str, Any]
    via: str
    author: dict[str, Any]
    sent_at: datetime
    expires_at: datetime
    seq: int | None = None

    @property
    def text(self) -> str | None:
        """The words it carries for L2 (a note's, an answer's), if any (rule R18)."""
        return self.payload.get("text") if self.kind in ("note", "answer") else None


def payload_problem(kind: str, payload: Any) -> str | None:
    """What is wrong with a request's payload for its kind, if anything: the payloads are
    closed shapes (spec 22 §3)."""
    if not isinstance(payload, dict):
        return "the payload is not an object"
    if unknown := sorted(set(payload) - _PAYLOAD[kind]):
        return f"unknown payload key(s) {unknown} for {kind}"
    for key, value in payload.items():
        if isinstance(value, str) and not clean(value, newlines=key == "text"):
            return f"{key} holds a control character"
    if kind == "budget":
        if amount(payload.get("usd")) is None or amount(payload["usd"]) <= 0:
            return "usd must be a plain decimal string above 0, such as \"15.00\""
    elif kind == "answer":
        if not _name(payload.get("ask_key")):
            return "ask_key must be a question's key"
        if "choice" in payload and not _name(payload["choice"], 200):
            return "choice must be a non-empty string"
        if "text" in payload and not _text(payload["text"]):
            return f"text must be 1 to {MAX_TEXT} characters"
        if "choice" not in payload and "text" not in payload:
            return "an answer needs a choice or a text"
    elif kind == "note":
        if not _text(payload.get("text")):
            return f"text must be 1 to {MAX_TEXT} characters"
        hypothesis = payload.get("hypothesis")
        if "hypothesis" in payload and (isinstance(hypothesis, bool)
                                        or not isinstance(hypothesis, int)):
            return "hypothesis must be an integer"
    return None


def _name(value: Any, limit: int = 100) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and value.strip() == value \
        and clean(value, newlines=False)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_TEXT


def _id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return value if str(parsed) == value.lower() else None


class _Big:
    """An integer of more than 40 digits in a line: no field takes one, so the line is
    malformed (and still acked, by its id), where ``int()`` would raise on 4300 digits."""


def _int(digits: str) -> int | _Big:
    return int(digits) if len(digits.lstrip("-")) <= 40 else _Big()


def parse_request(raw: bytes) -> tuple[Request | None, str | None, str | None]:
    """An inbox line as ``(request, id, problem)``: the request when it fits its shape,
    else ``None`` with the problem; ``id`` whenever it can be read, so a malformed line is
    acked."""
    try:
        line = json.loads(raw.decode("utf-8"), parse_int=_int)
    except (ValueError, RecursionError):  # a decode error, bad JSON, nesting too deep
        return None, None, "not JSON"
    if not isinstance(line, dict):
        return None, None, "not a JSON object"
    rid = _id(line.get("id"))
    if rid is None:
        return None, None, "no readable id"
    if len(raw) > MAX_LINE:
        return None, rid, f"longer than {MAX_LINE} bytes"
    if line.get("schema") != SCHEMA:
        return None, rid, f"schema is not {SCHEMA}"
    via = line.get("via")
    if not isinstance(via, str) or via not in _FIELDS:
        return None, rid, "via must be cosmos or local"
    if extra := sorted(set(line) - _FIELDS[via]):
        return None, rid, f"field(s) {extra} not in a {via} line"
    if missing := sorted(_FIELDS[via] - set(line)):
        return None, rid, f"field(s) {missing} missing"
    kind = line["kind"]
    if not isinstance(kind, str) or kind not in KINDS:
        return None, rid, f"unknown kind {escape(kind)[:40]!r}"
    author = line["author"]
    if not isinstance(author, dict) or set(author) != _AUTHOR[via] or not all(
            _name(v) for v in author.values()):
        return None, rid, f"author must be {sorted(_AUTHOR[via])}, each a short string"
    if via == "cosmos" and not _LOGIN.fullmatch(author["login"]):
        return None, rid, "author.login must be a GitHub login"
    sent, expires = parse_time(line["sent_at"]), parse_time(line["expires_at"])
    if sent is None or expires is None:
        return None, rid, "sent_at and expires_at must be RFC 3339 times in UTC"
    if expires > sent + LIFETIME + SKEW:
        return None, rid, "expires_at must be 15 minutes after sent_at at most"
    seq = line.get("seq")
    if via == "cosmos" and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
        return None, rid, "seq must be an integer >= 0"
    if problem := payload_problem(kind, line["payload"]):
        return None, rid, problem
    return Request(id=rid, kind=kind, payload=line["payload"], via=via, author=author,
                   sent_at=sent, expires_at=expires, seq=seq), rid, None


# ------------------------------------------------------------------ mailbox
def _me() -> int:
    return os.getuid()


def _checked(st: os.stat_result, path: Path, directory: bool) -> None:
    """``SteerError`` unless ``st`` is the user's own directory (0700 at most) or regular
    file (0600 at most)."""
    if directory and not stat.S_ISDIR(st.st_mode):
        raise SteerError(f"steering refused: {path} is not a directory")
    if not directory and not stat.S_ISREG(st.st_mode):
        raise SteerError(f"steering refused: {path} is not a regular file")
    if st.st_uid != _me():
        raise SteerError(f"steering refused: {path} is not owned by you")
    loose = st.st_mode & (0o077 if directory else 0o177)
    if loose:
        raise SteerError(f"steering refused: {path} has mode {stat.S_IMODE(st.st_mode):04o}, "
                         f"looser than {'0700' if directory else '0600'}")


def _open_error(error: OSError, path: Path) -> SteerError:
    if error.errno == errno.ELOOP or os.path.islink(path):
        return SteerError(f"steering refused: {path} is a symbolic link")
    if error.errno == errno.ENOTDIR:
        return SteerError(f"steering refused: {path} is not a directory")
    if error.errno == errno.ENXIO:  # a FIFO no one reads, opened to write
        return SteerError(f"steering refused: {path} is not a regular file")
    return SteerError(f"steering refused: {path} cannot be opened ({error.strerror})")


class Mailbox:
    """The mailbox directory, every open checked on its descriptor (``fstat``)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _dir(self, create: bool = False) -> int:
        if fcntl is None:
            raise SteerError(OFF_HERE)
        if create and not os.path.lexists(self.path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(FileExistsError):
                os.mkdir(self.path, 0o700)
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                         | os.O_CLOEXEC)
        except FileNotFoundError:
            raise SteerError(f"no mailbox at {self.path}") from None
        except OSError as error:
            raise _open_error(error, self.path) from None
        try:
            _checked(os.fstat(fd), self.path, directory=True)
        except SteerError:
            os.close(fd)
            raise
        return fd

    def check(self, create: bool = False) -> None:
        """``SteerError`` unless the directory (made when missing, with ``create``) and
        every file of it there is now pass the checks."""
        fd = self._dir(create)
        try:
            for name in (PROGRAM_FILE, INBOX, OUTBOX):
                with contextlib.suppress(FileNotFoundError):
                    os.close(self._file(fd, name, os.O_RDONLY))
        finally:
            os.close(fd)

    def _file(self, dir_fd: int, name: str, flags: int) -> int:
        path = self.path / name
        try:  # without blocking: a FIFO opens at once, to be refused, not waited on
            fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600,
                         dir_fd=dir_fd)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise _open_error(error, path) from None
        try:
            _checked(os.fstat(fd), path, directory=False)
            os.set_blocking(fd, True)
        except SteerError:
            os.close(fd)
            raise
        return fd

    def open(self, name: str, flags: int = os.O_RDONLY) -> int:
        """A checked descriptor on one of the mailbox's files."""
        dir_fd = self._dir()
        try:
            return self._file(dir_fd, name, flags)
        finally:
            os.close(dir_fd)

    def touch(self, name: str) -> None:
        os.close(self.open(name, os.O_WRONLY | os.O_CREAT | os.O_APPEND))

    def append(self, name: str, row: Mapping[str, Any]) -> None:
        """One line, appended with one ``write`` under an exclusive lock on the file."""
        data = (json.dumps(row) + "\n").encode()
        fd = self.open(name, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                written = os.write(fd, data)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        if written != len(data):
            raise SteerError(f"steering: only {written} of {len(data)} bytes were appended to "
                             f"{self.path / name}")

    def read(self, name: str, offset: int = 0) -> tuple[list[bytes], int]:
        """The complete lines from ``offset`` on, and the offset after the last newline
        (what follows it is read at the next look). No lock."""
        try:
            fd = self.open(name)
        except FileNotFoundError:
            return [], offset
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            chunks = []
            while chunk := os.read(fd, 1 << 16):
                chunks.append(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        end = data.rfind(b"\n")
        if end < 0:
            return [], offset
        return data[:end].split(b"\n"), offset + end + 1

    def write_program(self, program: Mapping[str, Any]) -> int:
        """Write ``program.json`` (a new file put in place by rename) and hold a shared lock
        on it while the audit reads: the command tells whether anyone is reading by that
        lock as well as by the process id. Returns the descriptor that holds the lock."""
        dir_fd = self._dir(create=True)
        try:
            temp = f".{PROGRAM_FILE}.{os.getpid()}.{uuid.uuid4().hex}"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
            try:
                os.write(fd, (json.dumps(program, indent=1) + "\n").encode())
            finally:
                os.close(fd)
            os.replace(temp, PROGRAM_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            fd = self._file(dir_fd, PROGRAM_FILE, os.O_RDONLY)
        finally:
            os.close(dir_fd)
        fcntl.flock(fd, fcntl.LOCK_SH)
        return fd

    def program(self) -> dict[str, Any]:
        """``program.json``; ``SteerError`` when there is none or it does not fit."""
        try:
            fd = self.open(PROGRAM_FILE)
        except FileNotFoundError:
            raise SteerError(f"no audit reads {self.path}: it has no {PROGRAM_FILE}") from None
        try:
            data = os.read(fd, MAX_LINE * 4)
        finally:
            os.close(fd)
        try:
            program = json.loads(data)
        except (ValueError, RecursionError):
            program = None
        if not isinstance(program, dict) or program.get("schema") != SCHEMA or not (
                isinstance(program.get("kinds"), list) and isinstance(program.get("pid"), int)
                and isinstance(program.get("limits"), dict)):
            raise SteerError(f"{self.path / PROGRAM_FILE} is not a {SCHEMA} program file")
        return program

    def reading(self, program: Mapping[str, Any]) -> bool:
        """Whether the program is still reading: its process is alive and holds its lock on
        ``program.json``."""
        try:
            os.kill(program["pid"], 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        except OSError:
            return False
        try:
            fd = self.open(PROGRAM_FILE)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)  # closing releases a lock it took
        return False

    def outbox(self) -> Outbox:
        lines, _ = self.read(OUTBOX)
        return Outbox.of(lines)


@dataclass
class Outbox:
    """What ``outbox.jsonl`` says: acked ids, questions and whether they are closed, the
    highest ask and say numbers. Lines that do not parse are skipped."""

    acks: list[dict[str, Any]] = field(default_factory=list)
    asks: dict[str, dict[str, Any]] = field(default_factory=dict)
    closed: dict[str, str] = field(default_factory=dict)
    numbers: dict[str, int] = field(default_factory=lambda: {"ask": 0, "say": 0})

    @classmethod
    def of(cls, lines: Sequence[bytes]) -> Outbox:
        found = cls()
        for raw in lines:
            try:
                row = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if not isinstance(row, dict) or row.get("schema") != SCHEMA:
                continue
            kind = row.get("type")
            if kind == "ack" and isinstance(row.get("id"), str):
                found.acks.append(row)
            elif kind == "ask" and isinstance(row.get("ask_key"), str):
                found.asks[row["ask_key"]] = row
            elif kind == "ask_closed" and isinstance(row.get("ask_key"), str):
                found.closed.setdefault(row["ask_key"], str(row.get("state")))
            for key in ("ask_key", "say_key"):
                if isinstance(row.get(key), str) and (m := _KEY.fullmatch(row[key])):
                    found.numbers[m[1]] = max(found.numbers[m[1]], int(m[2]))
        return found

    @property
    def acked(self) -> set[str]:
        return {row["id"] for row in self.acks}

    def open_asks(self) -> dict[str, dict[str, Any]]:
        return {key: ask for key, ask in self.asks.items() if key not in self.closed}


# ------------------------------------------------------------- the program
@dataclass
class Ask:
    key: str
    text: str
    choices: list[str]
    allow_text: bool
    default: str | None
    answer: Request | None = None


class Steering:
    """The audit's side of the mailbox (spec 22 §6): it writes ``program.json``, reads the
    inbox where the audit looks (``poll``: on L2's turns, before a launch, while a question
    waits), applies or refuses each line and records it: an ack in the outbox, a row in the
    audit's ``steer.jsonl``, and, for an applied line, an ``l2_steer`` event in L2's log
    (``flush``, from the event loop only).

    ``audit`` is the ``Audit`` (``interrupt``, ``pause``, ``resume``, ``raise_budget``,
    ``budget``, ``clock_state``, ``ended``, ``interrupts``, ``out``); ``step`` gives L2's
    tool call number now (``Tools`` sets it)."""

    def __init__(self, mailbox: Mailbox, audit: Any, *, max_budget: Decimal | None = None,
                 sandbox: dict[str, Any] | None = None, poll_s: float = POLL,
                 say: Callable[[str], None] = show) -> None:
        self.mailbox = mailbox
        self.audit = audit
        self.max_budget = max_budget
        self.sandbox = sandbox
        self.poll_s = poll_s
        self.say = say
        self.kinds = [k for k in KINDS if k in ("pause", "resume", "stop", "answer")
                      or (k == "budget" and max_budget is not None)
                      or (k == "note" and sandbox is not None)]
        self.step: Callable[[], int] = lambda: 0
        self.started_at: datetime | None = None
        self.offset = 0
        self.seen: set[str] = set()
        """Ids read already: acked, or a note waiting to be delivered."""
        self.asks: dict[str, Ask] = {}
        """Questions open now."""
        self.numbers = {"ask": 0, "say": 0}
        self.notes: list[Request] = []
        """Notes read and not yet delivered to L2."""
        self.delivered: set[str] = set()
        """Ids of the notes L2 was given (``reply_researcher`` may answer them)."""
        self.paused = False
        self.events: list[dict[str, Any]] = []
        """``l2_steer`` events not yet written to L2's log (``flush``)."""
        self.counts = {"applied": 0, "refused": 0, "steered": 0}
        self.raised: tuple[float, float] | None = None
        """A budget raise L2's token backstop has not followed yet: (from, to)."""
        self.ended = False
        self.broken: str | None = None
        """Why the mailbox is no longer read, if it failed its checks during the audit."""
        self._lock: int | None = None

    # --------------------------------------------------------- start and end
    def program(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "program": PROGRAM, "pid": os.getpid(),
                "started_at": stamp(self.started_at), "kinds": self.kinds,
                "sandbox": self.sandbox,
                "limits": {"max_budget_usd": None if self.max_budget is None
                           else usd(self.max_budget)}}

    def open(self) -> None:
        """The audit's first look: read the outbox (a reused mailbox: skip every id that
        has an ack, close a question left open, number questions and replies after it),
        make the files, write ``program.json``."""
        self.mailbox.check(create=True)
        self.started_at = parse_time(stamp(now()))  # to the millisecond, as lines are sent
        found = self.mailbox.outbox()
        self.seen |= found.acked
        self.numbers = dict(found.numbers)
        for key in found.open_asks():
            self._out({"type": "ask_closed", "ask_key": key, "state": "withdrawn"})
        for name in (INBOX, OUTBOX):
            self.mailbox.touch(name)
        self._lock = self.mailbox.write_program(self.program())
        self.say(f"steering: reading {escape(self.mailbox.path)} (takes "
                 f"{', '.join(self.kinds)}; "
                 + ("no sandbox: notes and text answers only from this machine)"
                    if self.sandbox is None else f"sandbox {escape(self.sandbox)})"))

    def close(self) -> None:
        """The audit has ended: every line still unapplied is refused (``ended``), a
        question still open is withdrawn, and the lock on ``program.json`` is let go."""
        self.ended = True
        try:
            self.drop_notes()
            self.poll()
            for key in list(self.asks):
                self._close_ask(key, "withdrawn")
        finally:
            if self.paused:
                self._unpause()
            if self._lock is not None:
                os.close(self._lock)
                self._lock = None

    def summary(self) -> dict[str, Any]:
        """What ``ledger.json`` keeps of the steering."""
        return {"mailbox": str(self.mailbox.path), "kinds": self.kinds,
                "sandbox": self.sandbox,
                "max_budget_usd": None if self.max_budget is None else usd(self.max_budget),
                **self.counts, "paused": self.paused,
                "budget_raises": list(getattr(self.audit, "raises", []))}

    # -------------------------------------------------------------- reading
    def poll(self) -> None:
        """Read the inbox's new lines and apply or refuse each."""
        if self.broken is not None:
            return
        try:
            lines, self.offset = self.mailbox.read(INBOX, self.offset)
        except SteerError as error:
            self._fail(error)
            return
        for raw in lines:
            try:
                self._take(raw)
            except Exception as error:  # one line must not stop the lines after it
                self.say(f"steering: an inbox line could not be read and is skipped "
                         f"({escape(type(error).__name__)}: {escape(error)[:200]})")

    def _fail(self, error: SteerError) -> None:
        """The mailbox failed its checks during the audit (e.g. its mode was changed): it is
        no longer read, a pause ends, and the audit goes on, as it would unsteered; the
        ledger and the report say so."""
        if self.broken is None:
            self.broken = str(error)
            self.say(f"steering: the mailbox is no longer read: {escape(error)}")
            self.audit.notes.append(f"steering stopped during the audit: {error}")

    def _take(self, raw: bytes) -> None:
        request, rid, problem = parse_request(raw)
        if rid is None or rid in self.seen:
            return  # a line with no readable id cannot be acked
        self.seen.add(rid)
        if request is None:
            self._refuse(rid, "malformed", None, problem)
            return
        reason = self._refusal(request)
        if reason is not None:
            self._refuse(rid, reason, request)
            return
        self._apply(request)

    def _refusal(self, request: Request) -> str | None:
        """Why ``request`` is not applied, if it is not: the checks the plugin makes, made
        again where the code runs (spec 22 §2 step 6)."""
        moment = now()
        if request.sent_at < self.started_at:
            return "before start"
        if request.expires_at <= moment or request.expires_at <= request.sent_at \
                or request.sent_at > moment + SKEW:
            return "expired"  # sent "later" than now: its times cannot be trusted
        if self.ended or self.audit.ended:
            return "ended"
        if request.via == "cosmos" and request.text is not None and self.sandbox is None:
            return "no sandbox"  # rule R18: text from cosmos waits for a sandbox
        if request.kind not in self.kinds and not (request.kind == "note"
                                                   and request.via == "local"):
            return "not implemented"
        if request.kind == "budget":
            wanted = Decimal(request.payload["usd"])
            if wanted <= Decimal(str(self.audit.budget()["budget_usd"])):
                return "not a raise"
            if wanted > self.max_budget:
                return "over the maximum"
        if request.kind == "answer":
            ask = self.asks.get(request.payload["ask_key"])
            if ask is None:
                return "closed"
            if "text" in request.payload and not ask.allow_text:
                return "not offered"
            if "choice" in request.payload and request.payload["choice"] not in ask.choices:
                return "not offered"
        return None

    def _apply(self, request: Request) -> None:
        if request.kind == "note":
            self.notes.append(request)  # applied when L2 gets it (``deliver``)
            return
        step = self.step()
        by = who(request.author)
        if request.kind == "stop":
            if self.paused:
                self._unpause()
            if not self.audit.interrupts:  # a second stop is not a harder stop
                self.audit.interrupt(f"stopped at a researcher's request ({by})")
        elif request.kind == "pause" and not self.paused:
            self.paused = True
            self.audit.pause()
        elif request.kind == "resume" and self.paused:
            self._unpause()
        elif request.kind == "budget":
            before = float(self.audit.budget()["budget_usd"])
            to = float(Decimal(request.payload["usd"]))
            self.audit.raise_budget(to, by=by, at_step=step)
            self.raised = (self.raised[0] if self.raised else before, to)
        elif request.kind == "answer":
            ask = self.asks[request.payload["ask_key"]]
            ask.answer = request
            if request.text is not None:
                self.say(f"steering: text to L2 from {escape(by)}, answering {ask.key}:\n"
                         + _indent(escape(request.text, newlines=True)))
            self._close_ask(ask.key, "answered")
        self._applied(request, step)

    def _unpause(self) -> None:
        self.paused = False
        self.audit.resume()

    # ------------------------------------------------------------ recording
    def _out(self, row: Mapping[str, Any]) -> None:
        if self.broken is not None:
            return
        try:
            self.mailbox.append(OUTBOX, {"schema": SCHEMA, **row})
        except SteerError as error:
            self._fail(error)

    def _ack(self, rid: str, state: str, request: Request | None, **more: Any) -> None:
        row: dict[str, Any] = {"type": "ack", "id": rid, "state": state, **more}
        if request is not None and request.via == "local":
            row.update(via="local", kind=request.kind, payload=request.payload,
                       author=request.author)
        self._out(row)

    def _record(self, rid: str, request: Request | None, state: str,
                **more: Any) -> None:
        row = {"time": stamp(now()), "id": rid,
               **({k: getattr(request, k) for k in ("kind", "via", "author", "payload")}
                  if request is not None else {"kind": None, "via": None, "author": None,
                                               "payload": None}),
               "text": None if request is None else request.text,
               "sent_at": None if request is None else stamp(request.sent_at),
               "state": state, **more}
        with (self.audit.out / "steer.jsonl").open("a") as file:
            file.write(json.dumps(row, sort_keys=True) + "\n")

    def _refuse(self, rid: str, reason: str, request: Request | None,
                problem: str | None = None) -> None:
        self._ack(rid, "refused", request, reason=reason)
        self._record(rid, request, "refused", reason=reason,
                     **({"problem": problem} if problem else {}))
        self.counts["refused"] += 1
        self.say(f"steering: refused {request.kind if request else 'a line'} {rid}"
                 + (f" from {escape(who(request.author))}" if request else "")
                 + f": {reason}" + (f" ({escape(problem)})" if problem else ""))

    def _applied(self, request: Request, step: int) -> None:
        self._ack(request.id, "applied", request, at_step=step)
        self._record(request.id, request, "applied", at_step=step)
        self.events.append({"event": EVENT, "version": EVENT_VERSION, "id": request.id,
                            "kind": request.kind, "via": request.via,
                            "author": request.author, "text": request.text,
                            "payload": request.payload, "at_step": step})
        self.counts["applied"] += 1
        if request.kind != "stop":
            self.counts["steered"] += 1
        self.say(f"steering: applied {request.kind} from {escape(who(request.author))} at "
                 f"step {step}" + (f" ({request.payload['usd']} USD)"
                                   if request.kind == "budget" else ""))

    def flush(self) -> None:
        """Write the applied lines' ``l2_steer`` events to L2's log. Only from the event
        loop: a worker thread (a launch) leaves them for the next flush."""
        from inspect_ai.log import transcript

        events, self.events = self.events, []
        for event in events:
            transcript().info(event, source="l2")

    # ---------------------------------------------------------------- waits
    def hold(self) -> None:
        """Before a launch (in its worker thread): look at the mailbox, and wait while the
        audit is paused, looking again about once a second. A stop, or a Ctrl-C, ends the
        wait."""
        self.poll()
        while self.paused and not self.audit.interrupts and self.broken is None:
            time.sleep(self.poll_s)
            self.poll()
        if self.paused:
            self._unpause()

    async def between_turns(self) -> None:
        """After L2's turn (``on_continue``): as ``hold``, without blocking the loop."""
        import anyio

        self.poll()
        while self.paused and not self.audit.interrupts and self.broken is None:
            await anyio.sleep(self.poll_s)
            self.poll()
        if self.paused:
            self._unpause()
        self.flush()

    def deliver(self) -> str | None:
        """The notes read and not yet given to L2, oldest first, in their frame; each is
        shown in the terminal first and recorded as applied at this step."""
        if not self.notes:
            return None
        notes, self.notes = self.notes, []
        step = self.step()
        parts = []
        for note in notes:
            by = who(note.author)
            self.say(f"steering: note to L2 from {escape(by)}:\n"
                     + _indent(escape(note.payload["text"], newlines=True)))
            self._applied(note, step)
            self.delivered.add(note.id)
            parts.append(frame(note))
        self.flush()
        return "\n\n".join(parts)

    def drop_notes(self) -> None:
        """L2 is ending: the notes it will not read are refused (``ended``)."""
        notes, self.notes = self.notes, []
        for note in notes:
            self._refuse(note.id, "ended", note)

    def take_raise(self) -> tuple[float, float] | None:
        """A budget raise for L2's token backstop to follow, once."""
        raised, self.raised = self.raised, None
        return raised

    # ------------------------------------------------------- L2's questions
    async def ask(self, question: Any, choices: Any, default: Any = None,
                  timeout_s: Any = None, allow_text: Any = False,
                  cost_usd: Any = None) -> str:
        """``ask_researcher``: write the question, wait for an answer, a stop or the
        timeout, close it and say what came of it."""
        import anyio

        if isinstance(choices, str):
            with contextlib.suppress(ValueError, RecursionError):
                choices = json.loads(choices)
        ask, timeout, cost = self._question(question, choices, default, timeout_s,
                                            allow_text, cost_usd)
        self._out({"type": "ask", "ask_key": ask.key, "text": ask.text,
                   "choices": ask.choices, "allow_text": ask.allow_text,
                   "default_choice": ask.default,
                   "cost": None if cost is None else {"usd": cost}, "timeout_s": timeout,
                   "asked_at": stamp(now())})
        self._sent("the question was not asked")
        self.asks[ask.key] = ask
        self.say(f"steering: L2 asks ({ask.key}): {escape(ask.text)}\n  choices: "
                 + ", ".join(escape(c) for c in ask.choices)
                 + (f"; default {escape(ask.default)}" if ask.default is not None else "")
                 + f"; answers itself in {timeout} s. Answer with: python -m "
                 f"experiments.l2.steer answer {ask.key} <choice>")
        until = time.monotonic() + timeout
        stopped = self.audit.interrupts
        while True:
            self.poll()
            if ask.answer is not None:
                break
            if self.audit.interrupts > stopped:
                self._close_ask(ask.key, "withdrawn")
                self.flush()
                return (f"Question {ask.key} was withdrawn: the audit is stopping "
                        f"({self.audit.halted}). Conclude now.")
            unread = self.broken is not None  # nobody's answer can arrive any more
            if unread or time.monotonic() >= until:
                self._close_ask(ask.key, "timed_out")
                self.flush()
                return ((f"Question {ask.key} can no longer be answered: the researchers' "
                         f"mailbox is no longer read ({self.broken}): " if unread
                         else f"No answer to {ask.key} within {timeout} s: ")
                        + (f"the default applies: {ask.default}." if ask.default is not None
                           else "there is no default, so decide yourself."))
            await anyio.sleep(min(self.poll_s, max(until - time.monotonic(), 0.0)))
        self.flush()
        answer = ask.answer
        lines = [f"Answer to {ask.key} from {who(answer.author)}, a researcher allowed to "
                 "direct this audit: " + (answer.payload["choice"] if "choice"
                                          in answer.payload else "(no choice; text only)")
                 + "."]
        if answer.text is not None:
            lines += ["The researcher's text (the brief and its limits still hold):", "",
                      answer.text]
        return "\n".join(lines)

    def _question(self, question: Any, choices: Any, default: Any, timeout_s: Any,
                  allow_text: Any, cost_usd: Any) -> tuple[Ask, int, str | None]:
        if self.ended or self.audit.ended:
            raise Refusal("refused: the audit has ended")
        if self.broken is not None:
            raise Refusal(f"refused: the researchers' mailbox is no longer read ({self.broken})")
        if self.audit.interrupts:
            raise Refusal(f"refused: the audit is stopping ({self.audit.halted}): conclude")
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_TEXT \
                or not clean(question):
            raise Refusal(f"question must be 1 to {MAX_TEXT} characters, with no control "
                          "characters")
        if not isinstance(choices, list) or not 0 < len(choices) <= MAX_CHOICES or not all(
                _name(c, 200) for c in choices) or len(set(choices)) != len(choices):
            raise Refusal(f"choices must be a list of 1 to {MAX_CHOICES} distinct non-empty "
                          "strings without control characters")
        if default is not None and default not in choices:
            raise Refusal(f"default must be one of the choices {choices}, or left out")
        if not isinstance(allow_text, bool):
            raise Refusal("allow_text must be true or false")
        timeout = ASK_TIMEOUT if timeout_s is None else timeout_s
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not (
                timeout > 0) or timeout != timeout:
            raise Refusal("timeout_s must be a number of seconds above 0")
        left = self.audit.clock_state()["time_left_s"]
        # whole seconds, as the outbox gives them
        timeout = max(1, math.ceil(min(float(timeout), ASK_MAX,
                                       *([max(left, 0.0)] if left is not None else []))))
        cost = None
        if cost_usd is not None:
            if isinstance(cost_usd, bool) or not isinstance(cost_usd, int | float) or not (
                    0 <= cost_usd < 1e9):
                raise Refusal("cost_usd must be an amount in dollars, or left out")
            cost = usd(Decimal(str(cost_usd)))
        self.numbers["ask"] += 1
        return (Ask(key=f"ask-{self.numbers['ask']}", text=question, choices=list(choices),
                    allow_text=allow_text, default=default), timeout, cost)

    def _sent(self, what: str) -> None:
        """After a line L2 is told about: refuse when the mailbox failed at that write, so
        L2 is never told a question or a reply went out when it did not."""
        if self.broken is not None:
            raise Refusal(f"refused: the researchers' mailbox is no longer read "
                          f"({self.broken}), so {what}")

    def _close_ask(self, key: str, state: str) -> None:
        self.asks.pop(key, None)
        self._out({"type": "ask_closed", "ask_key": key, "state": state})

    def reply(self, text: Any, note_id: Any = None) -> str:
        """``reply_researcher``: write a ``say`` line, in reply to a note L2 was given, or
        to nobody in particular."""
        if self.broken is not None:
            raise Refusal(f"refused: the researchers' mailbox is no longer read ({self.broken})")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_SAY:
            raise Refusal(f"text must be 1 to {MAX_SAY} characters")
        if note_id is not None and note_id not in self.delivered:
            raise Refusal(f"note_id {escape(note_id)!r} is not a note you were given"
                          + (f"; yours: {sorted(self.delivered)}" if self.delivered else ""))
        self.numbers["say"] += 1
        key = f"say-{self.numbers['say']}"
        self._out({"type": "say", "say_key": key, "in_reply_to": note_id, "text": text})
        self._sent("the reply was not sent")
        self.say(f"steering: L2 says ({key}): {escape(text)}")
        return f"Sent as {key}" + (f", in reply to note {note_id}." if note_id else ".")


def frame(note: Request) -> str:
    """A note as L2 reads it: who wrote it, that a researcher allowed to direct this audit
    wrote it, and that the brief's limits still hold. The frame is no boundary: it says who
    said what (spec 22 §7)."""
    hypothesis = note.payload.get("hypothesis")
    return "\n".join([
        f"A note from {who(note.author)}, a researcher allowed to direct this audit (note "
        f"{note.id}" + (f", on hypothesis {hypothesis}" if hypothesis is not None else "")
        + "). The brief and its limits still hold: the budget, the allowed changes and every "
        "refusal of your tools apply to what you do after it. You may answer it with "
        "reply_researcher.", "", note.payload["text"]])


def _indent(text: str) -> str:
    return "\n".join(f"  | {line}" for line in text.splitlines() or [""])


def configure(audit_out: Path, *, flag: bool = False,
              environ: Mapping[str, str] = os.environ
              ) -> tuple[Mailbox, Decimal | None] | None:
    """Whether and where the audit is steered, checked before anything is made: the
    mailbox and the maximum budget, or ``None`` when steering is off (asked for on a system
    without ``fcntl``: off, with one line). ``SteerError`` when it cannot be used: a
    mailbox that is a link, someone else's or open to others, or a maximum that is not a
    plain decimal string."""
    if not requested(flag, environ):
        return None
    if fcntl is None:
        show(OFF_HERE)
        return None
    limit = max_budget(environ)
    mailbox = Mailbox(directory(audit_out, environ))
    if os.path.lexists(mailbox.path):
        mailbox.check()
    return mailbox, limit


# ----------------------------------------------------------------- command
class _Arguments(Exception):
    """Arguments the command cannot read (exit 2)."""


def newest(root: Path = Path("logs/l2-audit")) -> Path | None:
    """The mailbox of the newest audit under ``root``: the one whose ``program.json`` (or
    the directory, if it has none) changed last."""
    found = []
    for path in root.glob(f"*/l2/{DIR_NAME}"):
        with contextlib.suppress(OSError):
            program = path / PROGRAM_FILE
            found.append(((program if os.path.lexists(program) else path).lstat().st_mtime,
                          path))
    return max(found)[1] if found else None


def _user() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return getpass.getuser()


def _ledger_total(mailbox: Mailbox) -> Decimal | None:
    """The audit's budget total now, from its ``ledger.json``, when the mailbox is in the
    audit's own ``l2/`` (with ``COSMOS_STEER_DIR`` the audit is not known: the program's
    own check decides)."""
    path = mailbox.path
    ledger = path.parent.parent / "ledger.json"
    if path.name != DIR_NAME or path.parent.name != "l2" or not ledger.is_file():
        return None
    with contextlib.suppress(ValueError, KeyError, TypeError, OSError):
        return Decimal(str(json.loads(ledger.read_text())["budget_usd"]))
    return None


def request_of(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """The command's request as a kind and its payload."""
    kind = args.command
    if kind == "note":
        return kind, {"text": args.text}
    if kind == "budget":
        if amount(args.usd) is None or amount(args.usd) <= 0:
            raise _Arguments(f"budget: {args.usd!r} is not a plain decimal amount above 0, "
                             "such as 15.00")
        return kind, {"usd": args.usd}
    if kind == "answer":
        return kind, {"ask_key": args.ask_key, "choice": args.choice,
                      **({"text": args.text} if args.text is not None else {})}
    return kind, {}


def refusal(mailbox: Mailbox, program: Mapping[str, Any], kind: str,
            payload: Mapping[str, Any]) -> str | None:
    """What the program would refuse, said before anything is written."""
    if problem := payload_problem(kind, dict(payload)):
        return f"refused: {problem}"
    if kind not in program["kinds"] and kind != "note":
        return (f"refused: the audit takes no {kind} request"
                + (": it was started without COSMOS_STEER_MAX_BUDGET_USD"
                   if kind == "budget" else ""))
    if kind == "budget":
        wanted = Decimal(payload["usd"])
        total = _ledger_total(mailbox)
        if total is not None and wanted <= total:
            return f"refused: not a raise: the budget is already ${usd(total)}"
        limit = amount(program["limits"].get("max_budget_usd"))
        if limit is not None and wanted > limit:
            return f"refused: over the maximum: the most it takes is ${usd(limit)}"
    if kind == "answer":
        found = mailbox.outbox()
        ask = found.open_asks().get(payload["ask_key"])
        if ask is None:
            return f"refused: {escape(payload['ask_key'])} is not an open question"
        if payload["choice"] not in (ask.get("choices") or []):
            return ("refused: not offered: choose one of "
                    + ", ".join(escape(c) for c in ask.get("choices") or []))
        if "text" in payload and not ask.get("allow_text"):
            return f"refused: not offered: {escape(payload['ask_key'])} takes no text"
    return None


def status(mailbox: Mailbox, as_json: bool) -> int:
    program = mailbox.program()
    state = "reading" if mailbox.reading(program) else "ended"
    found = mailbox.outbox()
    asks = [{"ask_key": key, "text": ask.get("text"), "choices": ask.get("choices"),
             "allow_text": ask.get("allow_text"), "default_choice": ask.get("default_choice"),
             "cost": ask.get("cost"), "timeout_s": ask.get("timeout_s"),
             "asked_at": ask.get("asked_at"), "left_s": _left(ask)}
            for key, ask in found.open_asks().items()] if state == "reading" else []
    acks = found.acks[-10:]
    if as_json:
        print(json.dumps({"dir": str(mailbox.path), "state": state,
                          "pid": program.get("pid"), "started_at": program.get("started_at"),
                          "kinds": program["kinds"], "sandbox": program.get("sandbox"),
                          "max_budget_usd": program["limits"].get("max_budget_usd"),
                          "open_questions": asks, "acks": acks}))
        return 0
    sandbox = program.get("sandbox")
    print(f"{escape(mailbox.path)}: {state} (pid {program.get('pid')}, since "
          f"{escape(program.get('started_at'))})")
    print(f"kinds: {', '.join(escape(k) for k in program['kinds'])}")
    print("sandbox: " + ("none: notes and text answers only from this machine"
                         if sandbox is None else escape(json.dumps(sandbox))))
    limit = program["limits"].get("max_budget_usd")
    print("maximum budget: " + (f"${escape(limit)}" if limit is not None else
                                "none (no budget requests)"))
    for ask in asks:
        print(f"open question {escape(ask['ask_key'])}: {escape(ask['text'])}\n  choices: "
              + ", ".join(escape(c) for c in ask["choices"] or [])
              + (f"; default {escape(ask['default_choice'])}"
                 if ask["default_choice"] is not None else "")
              + (f"; {ask['left_s']} s left before it answers itself"
                 if ask["left_s"] is not None else "")
              + ("; takes text" if ask["allow_text"] else ""))
    if not asks:
        print("open questions: none")
    print(f"last acks ({len(acks)}):" if acks else "acks: none")
    for ack in acks:
        print(f"  {escape(ack.get('id'))} {escape(ack.get('state'))}"
              + (f" at step {escape(ack['at_step'])}" if "at_step" in ack else "")
              + (f": {escape(ack['reason'])}" if "reason" in ack else "")
              + (f" ({escape(ack['kind'])}, sent on the machine)" if "kind" in ack else ""))
    return 0


def _left(ask: Mapping[str, Any]) -> int | None:
    """Whole seconds before an open question answers itself, from its ``asked_at`` and
    ``timeout_s`` (0 once that has passed); ``None`` when either cannot be read."""
    asked, timeout = parse_time(ask.get("asked_at")), ask.get("timeout_s")
    if asked is None or isinstance(timeout, bool) or not isinstance(timeout, int | float):
        return None
    return max(0, math.ceil(timeout - (now() - asked).total_seconds()))


def parser() -> argparse.ArgumentParser:
    made = argparse.ArgumentParser(
        prog="python -m experiments.l2.steer",
        description="Steer a running L2 audit through its mailbox (Cosmos spec 22 §5.1).")
    made.add_argument("--dir", type=Path, default=None,
                      help="the mailbox (default: COSMOS_STEER_DIR, else the newest "
                           "logs/l2-audit/*/l2/.cosmos-steer)")
    commands = made.add_subparsers(dest="command", required=True)
    commands.add_parser("note", help="a note L2 reads at its next step").add_argument("text")
    for name, what in (("pause", "no new experiment starts, and L2 waits"),
                       ("resume", "end a pause"),
                       ("stop", "the graceful stop: the running experiment finishes, then "
                                "L2 concludes")):
        commands.add_parser(name, help=what)
    commands.add_parser("budget", help="raise the audit's total to USD").add_argument("usd")
    answer = commands.add_parser("answer", help="answer an open question of L2's")
    answer.add_argument("ask_key")
    answer.add_argument("choice")
    answer.add_argument("--text", default=None)
    status_ = commands.add_parser("status", help="the program, its questions and acks")
    status_.add_argument("--json", action="store_true")
    return made


def main(argv: Sequence[str] | None = None) -> int:
    """The local command (spec 22 §5.1): 0 and ``written <id>``; 1 and one line naming the
    reason it refused; 2 for arguments it cannot read."""
    made = parser()
    args = made.parse_args(argv)
    try:
        kind, payload = request_of(args) if args.command != "status" else ("status", {})
    except _Arguments as error:
        made.error(str(error))
    if fcntl is None:
        print(OFF_HERE, file=sys.stderr)
        return 1
    where = args.dir or (Path(os.environ["COSMOS_STEER_DIR"])
                         if os.environ.get("COSMOS_STEER_DIR") else newest())
    if where is None:
        print("no mailbox: give --dir, set COSMOS_STEER_DIR, or start an audit with "
              "steering on", file=sys.stderr)
        return 1
    mailbox = Mailbox(where)
    try:
        mailbox.check()
        if kind == "status":
            return status(mailbox, args.json)
        program = mailbox.program()
        if not mailbox.reading(program):
            raise SteerError(f"no audit reads {mailbox.path}: the audit that wrote its "
                             f"{PROGRAM_FILE} has ended")
        if why := refusal(mailbox, program, kind, payload):
            raise SteerError(why)
        if "text" in payload and program.get("sandbox") is None:
            print(NO_SANDBOX, file=sys.stderr)
        sent = now()
        rid = str(uuid.uuid4())
        mailbox.append(INBOX, {"schema": SCHEMA, "id": rid, "kind": kind, "payload": payload,
                               "via": "local", "author": {"local": _user()},
                               "sent_at": stamp(sent), "expires_at": stamp(sent + LIFETIME)})
    except SteerError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"written {rid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
