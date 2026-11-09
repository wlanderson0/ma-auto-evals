"""The Lean model's `-- mirrors:` references still point at the Python they mirror.

Every `name file.py:a-b` reference in `lean/Scope/Spec/*.lean` must find `def name` or
`class name` within those lines (a few lines of slack for decorators), and every bare
`file.py:a-b` must be a range inside the file. A moved function fails this test, so the
model gets re-read when the Python it mirrors changes (plan step 7).
"""

from __future__ import annotations

import re
from pathlib import Path

import orbit
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = sorted((ROOT / "lean" / "Scope" / "Spec").glob("*.lean"))
ORBIT = Path(orbit.__file__).parent
REF = re.compile(r"(?:([A-Za-z_][\w.]*)[,:]?\s+)?((?:[\w/]+/)?\w+\.py):(\d+)(?:-(\d+))?")
NOT_NAMES = {"and", "at", "of", "mirrors", "see", "in", "then", "also", "with", "the", "from",
             "Orbit", "Python", "e.g", "(", "lines", "ClassVar"}


def resolve(name: str) -> Path | None:
    for base in (ROOT / "src" / "orbit_audit", ROOT / "src" / "orbit_audit" / "auditor",
                 ORBIT, ROOT / "experiments"):
        if (base / name).exists():
            return base / name
    return None


def references() -> list[tuple[str, int, str | None, str, int, int]]:
    refs = []
    for spec in SPEC:
        for number, line in enumerate(spec.read_text().splitlines(), 1):
            if "--" not in line and "py:" not in line:
                continue
            for name, path, start, end in REF.findall(line):
                refs.append((spec.name, number, name or None, path, int(start),
                             int(end or start)))
    return refs


REFS = references()


def test_there_are_references():
    assert len(REFS) > 40


@pytest.mark.parametrize("ref", REFS, ids=[f"{r[0]}:{r[1]}:{r[3]}:{r[4]}" for r in REFS])
def test_reference_is_current(ref):
    spec, number, name, path, start, end = ref
    source = resolve(path)
    assert source is not None, f"{spec}:{number}: no file {path}"
    lines = source.read_text().splitlines()
    assert 1 <= start <= end <= len(lines), f"{spec}:{number}: {path}:{start}-{end} out of range"
    if name is None or name in NOT_NAMES or name[0].islower() and "." not in name:
        return
    leaf = name.split(".")[-1]
    window = "\n".join(lines[max(0, start - 4):end])
    pattern = rf"\b(def|class)\s+{re.escape(leaf)}\b|^\s*{re.escape(leaf)}\s*[:=]"
    assert re.search(pattern, window, re.MULTILINE), (
        f"{spec}:{number}: {name} is not defined in {path}:{start}-{end}")
