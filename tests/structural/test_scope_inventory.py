"""Every site that can change what agents see, or the scope, is reviewed (plan assumption T1)."""

from __future__ import annotations

import ast

from tests.structural.scope_inventory import _Visitor, diff, load, scan


def test_every_site_is_in_the_inventory():
    new, gone = diff(scan(), load())
    assert not new, (
        "unreviewed sites; check each goes through AuditController._decide, then add it to "
        "lean/scope-inventory.json with its role:\n"
        + "\n".join(f"  {s.file}:{s.line} {s.function} [{s.kind}] {s.expr}" for s in new))
    assert not gone, "listed sites no longer found; remove them:\n" + "\n".join(
        f"  {e['file']} {e['function']} [{e['kind']}] {e['expr']}" for e in gone)


def test_every_listed_site_has_a_role():
    assert all(entry["role"].strip() for entry in load())


def test_the_scan_finds_bypasses():
    source = """
def bypass(controller, runtime, op, state):
    controller.overlay.drops.add(("m1", None))
    controller.scope = None
    runtime.schedule.amend_pending([["alice"]], actor="x", reason="", expected_revision=0)
    state.messages.append(op)
    setattr(controller, "scope", None)
    op.apply(controller.overlay, None)
    del controller.overlay.rewrites["m1", None]
"""
    visitor = _Visitor("bypass.py")
    visitor.visit(ast.parse(source))
    assert [(s.kind, s.line) for s in visitor.sites] == [
        ("mutate", 3), ("write", 4), ("apply-call", 5), ("mutate", 6), ("mutate", 7),
        ("apply-call", 8), ("write", 9)]


def test_moving_code_does_not_change_the_keys():
    found = scan()
    assert all(site.line > 0 for site in found)
    assert len({site.key() for site in found}) == len(found)
