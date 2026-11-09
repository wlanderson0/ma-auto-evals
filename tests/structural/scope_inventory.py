"""AST inventory of code that can change what agents see, or the audit's scope.

Backs assumption T1 of the Lean scope plan (``lean/scope-map.md``): the world
changes only at the controller's apply sites. Every call that applies an op or
amends the schedule, and every write to the delivery overlay, the schedule, the
scope or a message store, in ``src/orbit_audit`` and ``experiments``, must be
listed in ``lean/scope-inventory.json`` with its role. A new one fails
``test_scope_inventory.py`` until it is reviewed and added.

Sites are keyed by file, enclosing function and source text, not by line, so
moving code does not churn the inventory. Run ``python -m
tests.structural.scope_inventory`` to print the current sites as JSON.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src/orbit_audit", "experiments")
INVENTORY = ROOT / "lean" / "scope-inventory.json"

APPLY_CALLS = frozenset({"apply", "produce", "amend_pending", "replace_remaining_agents", "halt",
                         "_apply_record", "_apply_message"})
"""Methods that apply an op, change the schedule or end the run, plus the controller's
record of an applied op (which marks apply sites, such as tool ops, that call nothing)."""
STATE_ATTRS = frozenset({"overlay", "rewrites", "drops", "injections", "delivered", "scope",
                         "schedule", "pending_steps"})
"""Attributes holding delivery, schedule or scope state."""
MUTATORS = frozenset({"append", "extend", "insert", "update", "add", "discard", "remove", "pop",
                      "popitem", "clear", "setdefault", "__setitem__", "__setattr__"})


@dataclass(frozen=True, order=True)
class Site:
    file: str
    function: str
    kind: str
    """``apply-call``, ``write`` (assignment or deletion) or ``mutate`` (mutating method)."""
    expr: str
    line: int = 0

    def key(self) -> tuple[str, str, str, str]:
        return (self.file, self.function, self.kind, self.expr)


def _chain(node: ast.AST) -> list[str]:
    """Attribute names along ``a.b[c].d`` from the outside in, with the root name last."""
    names: list[str] = []
    while True:
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            if isinstance(node, ast.Name):
                names.append(node.id)
            return names


def _watched(node: ast.AST) -> bool:
    names = _chain(node)
    if STATE_ATTRS.intersection(names):
        return True
    # A message store: ``<x>.state.messages`` (Inspect's conversation or Orbit's channel state).
    return any(a == "messages" and b == "state" for a, b in zip(names, names[1:], strict=False))


class _Visitor(ast.NodeVisitor):
    def __init__(self, file: str) -> None:
        self.file = file
        self.scope: list[str] = []
        self.sites: list[Site] = []

    def _add(self, kind: str, node: ast.AST) -> None:
        function = ".".join(self.scope) or "<module>"
        self.sites.append(Site(self.file, function, kind, ast.unparse(node), node.lineno))

    def _nested(self, node: ast.AST) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _nested

    def _targets(self, targets: list[ast.AST], node: ast.AST) -> None:
        for target in targets:
            elements = target.elts if isinstance(target, ast.Tuple | ast.List) else [target]
            if any(isinstance(t, ast.Attribute | ast.Subscript) and _watched(t)
                   for t in elements):
                self._add("write", node)
                return

    def visit_Assign(self, node: ast.Assign) -> None:
        self._targets(node.targets, node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._targets([node.target], node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._targets([node.target], node)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        self._targets(node.targets, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in APPLY_CALLS:
                self._add("apply-call", node)
            elif func.attr in MUTATORS and _watched(func.value):
                self._add("mutate", node)
        elif isinstance(func, ast.Name) and func.id == "setattr":
            self._add("mutate", node)
        self.generic_visit(node)


def scan(root: Path = ROOT) -> list[Site]:
    sites: list[Site] = []
    for top in SCANNED:
        for path in sorted((root / top).rglob("*.py")):
            file = path.relative_to(root).as_posix()
            visitor = _Visitor(file)
            visitor.visit(ast.parse(path.read_text(), filename=file))
            sites.extend(visitor.sites)
    return sorted(sites)


def load(path: Path = INVENTORY) -> list[dict]:
    return json.loads(path.read_text())["sites"]


def diff(found: list[Site], listed: list[dict]) -> tuple[list[Site], list[dict]]:
    """Sites found but not listed, and listed entries no longer found (respecting counts)."""
    want = Counter((e["file"], e["function"], e["kind"], e["expr"]) for e in listed)
    have = Counter(site.key() for site in found)
    new = [s for s in found if have[s.key()] > want[s.key()]]
    gone = [e for e in listed
            if want[(e["file"], e["function"], e["kind"], e["expr"])]
            > have[(e["file"], e["function"], e["kind"], e["expr"])]]
    return new, gone


def main() -> None:
    sites = [{"file": s.file, "function": s.function, "kind": s.kind, "expr": s.expr,
              "line": s.line} for s in scan()]
    json.dump(sites, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
