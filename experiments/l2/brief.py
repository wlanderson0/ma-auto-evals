"""Parse an L2 brief (spec ``docs/level2-interface.md`` §2).

A brief is a Markdown file: an optional YAML header between ``---`` lines, then
the research question in plain text. ``budget_usd`` is the only required
parameter; unknown keys are errors, so a typo never silently means "L2 decides".

Each variant's ``inner`` is merged over the brief's ``inner`` field by field, so
a grid can set ``condition`` once and give each variant its own ``opening``.
Top-level keys starting with ``x-`` are ignored, so shared texts can be defined
once as YAML anchors and referenced by every variant that uses them.

Scenario names are checked against the seed scenarios and the ``written`` ones
(``experiments.l2.written``) passed in; a written scenario is always an allowed base,
whatever ``base_scenarios`` says.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml  # PyYAML

from experiments.l2 import written as written_
from experiments.l2.model import (
    AXES,
    CONDITIONS,
    MEASURED,
    MODES,
    SCENARIO,
    START,
    InnerSpec,
    L2Brief,
    Mutation,
    Variant,
)
from experiments.scenarios import SCENARIOS
from orbit_audit.branches import AuditLimits
from orbit_audit.scope import AuditScope

HEADER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)", re.S | re.M)
BRIEF_KEYS = {"budget_usd", "run_budget_usd", "mode", "base_scenarios", "base_logs", "measure",
              "allowed_changes", "variants", "controls", "hypotheses", "inner"}
VARIANT_KEYS = {"id", "base", "mutations", "inner", "rationale"}
MUTATION_KEYS = {"axis", "op", "args", "at"}
INNER_KEYS = {"condition", "brief", "opening", "scope", "limits"}
SCOPE_KEYS = {"ops", "agents"}
VARIANT_ID = re.compile(r"[A-Za-z0-9_-]+")
EXTENSION = "x-"
"""Top-level header keys starting with this are ignored: a place to define YAML
anchors (shared openings, persona lines) that variants then reference."""


class BriefError(ValueError):
    """The brief cannot be parsed; the message says where and why."""


class _StrictLoader(yaml.SafeLoader):
    """PyYAML's safe loader, but a key repeated in one mapping is an error (PyYAML
    otherwise keeps the last one silently)."""


def _no_duplicates(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def parse_brief(path: str | Path,
                written: written_.WrittenScenarios = written_.NONE) -> L2Brief:
    text = Path(path).read_text(encoding="utf-8-sig")
    match = HEADER.match(text)
    if match is None and re.match(r"---[ \t]*\n", text):
        raise BriefError(f"{path}: the YAML header is not closed by a '---' line")
    try:
        header: Any = yaml.load(match[1], Loader=_StrictLoader) if match else None
    except yaml.YAMLError as error:
        raise BriefError(f"{path}: the YAML header does not parse: {error}") from error
    body = text[match.end():] if match else text
    if header is None:
        header = {}
    if not isinstance(header, Mapping):
        raise BriefError(f"{path}: the YAML header must be a mapping")
    header = {key: value for key, value in header.items()
              if not (isinstance(key, str) and key.startswith(EXTENSION))}
    _known(header, BRIEF_KEYS, "brief")
    if "budget_usd" not in header:
        raise BriefError(f"{path}: budget_usd is required (the budget is enforced, spec §7)")
    budget = header["budget_usd"]
    if (isinstance(budget, bool) or not isinstance(budget, int | float)
            or not math.isfinite(budget) or budget <= 0):
        raise BriefError(f"{path}: budget_usd must be a positive number, got {budget!r}")
    runs = header.get("run_budget_usd")
    if runs is not None and (isinstance(runs, bool) or not isinstance(runs, int | float)
                             or not math.isfinite(runs) or not 0 < runs <= budget):
        raise BriefError(f"{path}: run_budget_usd must be a positive number no larger than "
                         f"budget_usd ({budget}), got {runs!r}")
    question = body.strip()
    if not question:
        raise BriefError(f"{path}: the question (the text after the header) is empty")
    mode = header.get("mode")
    if mode is not None and mode not in MODES:
        raise BriefError(f"{path}: mode must be one of {list(MODES)}, got {mode!r}")
    bases = _strings(header, "base_scenarios")
    if bases is not None and (unknown := sorted(b for b in bases
                                                if not written_.known(b, written))):
        raise BriefError(f"{path}: unknown base_scenarios {unknown} "
                         f"(known: {sorted({*SCENARIOS, *written})})")
    allowed = _strings(header, "allowed_changes")
    if allowed is not None and (unknown := sorted(set(allowed) - set(AXES) - set(MEASURED)
                                                  - {SCENARIO})):
        raise BriefError(f"{path}: unknown allowed_changes {unknown} "
                         f"(axes: {list(AXES)}; measured: {list(MEASURED)}; or "
                         f"{SCENARIO!r}, writing scenarios)")
    measure = header.get("measure")
    if measure is not None and not isinstance(measure, str):
        raise BriefError(f"{path}: measure must be text")
    default = header.get("inner")
    inner = _inner(default, None, "inner") if default is not None else None
    variants = None
    if "variants" in header and header["variants"] is not None:
        variants = _variants(header["variants"], default, bases, written=written)
    controls = None
    if "controls" in header and header["controls"] is not None:
        controls = _variants(header["controls"], default, bases, kind="controls",
                             written=written)
        if variants and (both := sorted({v.id.casefold() for v in variants}
                                        & {c.id.casefold() for c in controls})):
            raise BriefError(f"{path}: ids {both} name both a variant and a control")
    return L2Brief(
        question=question, budget_usd=float(budget),
        run_budget_usd=None if runs is None else float(runs), mode=mode, base_scenarios=bases,
        base_logs=_strings(header, "base_logs"), measure=measure, allowed_changes=allowed,
        variants=variants, controls=controls, hypotheses=_strings(header, "hypotheses"),
        inner=inner)


def _known(data: Mapping[str, Any], keys: set[str], where: str) -> None:
    if unknown := sorted(set(data) - keys, key=str):
        raise BriefError(f"{where}: unknown key(s) {unknown} (allowed: {sorted(keys)})")


def _strings(data: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BriefError(f"{key} must be a list of strings, got {value!r}")
    return tuple(value)


def _variants(data: Any, default: Any, bases: tuple[str, ...] | None,
              kind: str = "variants",
              written: written_.WrittenScenarios = written_.NONE) -> tuple[Variant, ...]:
    """The brief's ``variants`` (or ``controls``, which have the same format)."""
    if not isinstance(data, list) or not data:
        raise BriefError(f"{kind} must be a non-empty list (leave it out to let L2 design "
                         "them)" if kind == "variants" else f"{kind} must be a non-empty list")
    variants, seen = [], set()
    for i, entry in enumerate(data):
        where = f"{kind}[{i}]"
        if not isinstance(entry, Mapping):
            raise BriefError(f"{where} must be a mapping")
        _known(entry, VARIANT_KEYS, where)
        for key in ("id", "base", "rationale"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise BriefError(f"{where}: {key} is required text")
        vid = entry["id"]
        where = f"{kind[:-1]} {vid}"
        if not VARIANT_ID.fullmatch(vid):
            raise BriefError(f"{where}: id may only use letters, digits, '_' and '-'")
        if vid.casefold() in seen:  # case-insensitive: ids name log folders
            raise BriefError(f"{where}: duplicate id")
        seen.add(vid.casefold())
        base = entry["base"]
        if not written_.known(base, written):
            raise BriefError(f"{where}: unknown base {base!r} "
                             f"(known: {sorted({*SCENARIOS, *written})})")
        if bases is not None and base not in bases and base not in written:
            raise BriefError(f"{where}: base {base!r} is not in base_scenarios {list(bases)}")
        if default is None and entry.get("inner") is None:
            raise BriefError(f"{where}: no inner spec (none in the variant or the brief)")
        mutations = [] if entry.get("mutations") is None else entry["mutations"]
        if not isinstance(mutations, list):
            raise BriefError(f"{where}: mutations must be a list")
        variants.append(Variant(
            id=vid, base=base,
            mutations=tuple(_mutation(m, f"{where} mutations[{j}]")
                            for j, m in enumerate(mutations)),
            inner=_inner(entry.get("inner"), default, f"{where} inner"),
            rationale=entry["rationale"].strip()))
    return tuple(variants)


def _mutation(data: Any, where: str) -> Mutation:
    if not isinstance(data, Mapping):
        raise BriefError(f"{where} must be a mapping")
    _known(data, MUTATION_KEYS, where)
    for key in ("axis", "op"):
        if not isinstance(data.get(key), str):
            raise BriefError(f"{where}: {key} is required text")
    if data["axis"] not in AXES:
        raise BriefError(f"{where}: unknown axis {data['axis']!r} (axes: {list(AXES)})")
    args = {} if data.get("args") is None else data["args"]
    if not isinstance(args, Mapping):
        raise BriefError(f"{where}: args must be a mapping")
    at = data.get("at", START)
    if not isinstance(at, str):
        raise BriefError(f"{where}: at must be text")
    # A deep copy: YAML anchors share nested values between the variants using them.
    return Mutation(axis=data["axis"], op=data["op"], args=copy.deepcopy(dict(args)), at=at)


def _inner(data: Any, default: Any, where: str) -> InnerSpec:
    """``data`` merged over ``default`` (both raw YAML mappings, or None)."""
    merged: dict[str, Any] = {}
    for layer, name in ((default, "inner"), (data, where)):
        if layer is None:
            continue
        if not isinstance(layer, Mapping):
            raise BriefError(f"{name} must be a mapping")
        _known(layer, INNER_KEYS, name)
        merged.update(layer)
    condition = merged.get("condition")
    if condition not in CONDITIONS:
        raise BriefError(f"{where}: condition must be one of {list(CONDITIONS)}, "
                         f"got {condition!r}")
    brief = merged.get("brief")
    if brief is not None and not isinstance(brief, str):
        raise BriefError(f"{where}: brief must be text")
    opening = _opening([] if merged.get("opening") is None else merged["opening"], where)
    if opening and condition != "scripted":
        raise BriefError(f"{where}: an opening is only played by the scripted condition, "
                         f"not {condition!r}")
    return InnerSpec(condition=condition, brief=brief, opening=opening,
                     scope=_scope(merged.get("scope"), where),
                     limits=_limits(merged.get("limits"), where))


def _opening(data: Any, where: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(data, list):
        raise BriefError(f"{where}: opening must be a list of [channel, text] pairs")
    pairs = []
    for message in data:
        if (not isinstance(message, list) or len(message) != 2
                or not all(isinstance(part, str) and part.strip() for part in message)):
            raise BriefError(f"{where}: opening message {message!r} is not a "
                             "[channel, text] pair")
        pairs.append((message[0].strip(), message[1].strip()))
    return tuple(pairs)


def _scope(data: Any, where: str) -> AuditScope | None:
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise BriefError(f"{where}: scope must be a mapping")
    _known(data, SCOPE_KEYS, f"{where} scope")
    if missing := sorted(SCOPE_KEYS - set(data)):
        raise BriefError(f"{where}: scope needs {missing} (null means no limit)")
    for key in sorted(SCOPE_KEYS):
        value = data[key]
        if value is not None and (not isinstance(value, list)
                                  or not all(isinstance(v, str) for v in value)):
            raise BriefError(f"{where}: scope {key} must be a list of names or null, "
                             f"got {value!r}")
    try:
        return AuditScope(ops=data.get("ops"), agents=data.get("agents"))
    except ValueError as error:
        raise BriefError(f"{where}: invalid scope: {error}") from error


def _limits(data: Any, where: str) -> AuditLimits | None:
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise BriefError(f"{where}: limits must be a mapping")
    bad = {k: v for k, v in data.items()
           if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0)}
    if bad:
        raise BriefError(f"{where}: limits must be whole numbers >= 0 or null, got {bad}")
    try:
        return AuditLimits(**data)
    except (TypeError, ValueError) as error:
        raise BriefError(f"{where}: invalid limits: {error}") from error
