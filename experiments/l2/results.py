"""Read an L2 audit's results from its eval logs (spec ``docs/level2-interface.md`` §6.1).

``Results.load(audit_dir)`` reads every eval log listed in ``runs.jsonl`` (the
record only says where the logs are; every measurement comes from the scorers in
the logs). Each sample's epoch is L2's, from the task metadata (``l2.epoch``):
each log holds one epoch, so ``sample.epoch`` is always 1.

- ``scores(variant_ids, scorer, by="agent")``: per-target values across epochs;
  a score that could not be made shows as ``INCOMPLETE``, never as a zero.
- ``compare(a, b, scorer, rounds=None, field=None)``: per target, k of n for each
  variant, the difference in rates, and n. ``scorer`` is ``"probe"`` (adopted: the
  probe's adopted answer, e.g. NOT_REQUIRED), ``"counts"`` (at least one use of the
  mention in the target's own words) or ``"outcome"`` (the outcome function's
  ``field`` is > 0 or true); with ``rounds``, ``"counts"`` is read from
  ``round_counts`` up to that round, so variants with different rounds are
  compared at the rounds they share (spec §6.3). Incomplete samples are left out
  of n and counted separately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from inspect_ai.log import read_eval_log

from experiments.l2 import written as written_
from experiments.scenarios import Scenario

INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class SampleResult:
    """One sample of one variant: its L2 identity and its scores' metadata."""

    variant_id: str
    base: str
    condition: str
    epoch: int
    scores: dict[str, dict[str, Any] | None]
    """Score name -> metadata; ``None`` when the score is missing or incomplete."""
    why: dict[str, str] = field(default_factory=dict)
    """Score name -> why it is missing or incomplete."""
    error: str | None = None
    log: str = ""


def sample_results(path: str) -> list[SampleResult]:
    log = read_eval_log(path)
    meta = (log.eval.metadata or {}).get("l2")
    if meta is None:
        raise ValueError(f"{path} is not an L2 variant's log (no l2 task metadata)")
    results = []
    for sample in log.samples or []:
        scores: dict[str, dict[str, Any] | None] = {}
        why: dict[str, str] = {}
        for name, score in (sample.scores or {}).items():
            metadata = dict(score.metadata or {})
            if INCOMPLETE in metadata:
                scores[name], why[name] = None, str(metadata[INCOMPLETE])
            else:
                scores[name] = metadata
        results.append(SampleResult(
            variant_id=meta["variant_id"], base=meta["base"], condition=meta["condition"],
            epoch=meta["epoch"], scores=scores, why=why,
            error=sample.error.message if sample.error else None, log=path))
    return results


def _final(meta: dict[str, Any]) -> dict[str, Any]:
    return meta["branches"][meta["final"]]["agents"]


def own_uses(sample: SampleResult, agent: str, rounds: int | None = None) -> int | None:
    """The target's uses of the mention in its own words (whole run, or up to
    ``rounds``); ``None`` when unknown."""
    if rounds is None:
        counts = sample.scores.get("counts")
        if counts is None:
            return None
        return _final(counts).get(agent, {}).get("own_mentions")
    per_round = sample.scores.get("round_counts")
    if per_round is None:
        return None
    if rounds > per_round["rounds"]:
        raise ValueError(f"{sample.variant_id} has {per_round['rounds']} rounds, not {rounds}")
    return sum(per_round["agents"][agent][:rounds])


def probe_answer(sample: SampleResult, agent: str) -> str | None:
    probe = sample.scores.get("probe")
    if probe is None:
        return None
    return (probe.get("agents", {}).get(agent) or {}).get("answer")


VALUES = {
    "probe": probe_answer,
    "counts": own_uses,
    "post_judge": lambda s, a: (None if s.scores.get("post_judge") is None
                                else s.scores["post_judge"]["agents"][a]["flagged_posts"]),
    "round_counts": lambda s, a: (None if s.scores.get("round_counts") is None
                                  else s.scores["round_counts"]["agents"][a]),
    "outcome": lambda s, a: (None if s.scores.get("post_judge") is None
                             else s.scores["post_judge"]["agents"][a]["outcome"]),
}
"""Per-target value of each scorer, from a sample."""


@dataclass
class Results:
    samples: list[SampleResult]
    variants: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Variant id -> its ``variants.jsonl`` record (problems included)."""
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Variant id -> its ``runs.jsonl`` record (cost, logs, errors)."""
    problems: list[str] = field(default_factory=list)
    """What is wrong with the logs themselves (missing, not one sample, repeated epochs)."""
    written: written_.WrittenScenarios = field(default_factory=lambda: written_.NONE)
    """The written scenarios the variants may be built on (``experiments.l2.written``). A
    factory, since Python 3.11 refuses a mapping as a dataclass default."""

    @classmethod
    def load(cls, audit: str | Path,
             written: written_.WrittenScenarios = written_.NONE) -> Results:
        audit = Path(audit)
        variants = {row["id"]: row for row in _jsonl(audit / "variants.jsonl")}
        runs = {row["variant"]["id"]: row for row in _jsonl(audit / "runs.jsonl")}
        samples, problems = [], []
        for vid, row in runs.items():
            for path in row["logs"]:
                resolved = _resolve(path, audit)
                if not resolved.exists():
                    problems.append(f"{vid}: log {path} is missing")
                    continue
                found = sample_results(str(resolved))
                if len(found) != 1:
                    problems.append(f"{vid}: log {path} holds {len(found)} samples, not 1")
                samples.extend(found)
        results = cls(samples=samples, variants=variants, runs=runs, problems=problems,
                      written=written)
        for vid in {s.variant_id for s in samples}:
            epochs = results.epochs(vid)
            if len(epochs) != len(set(epochs)):
                problems.append(f"{vid}: epochs {epochs} repeat (two samples share an epoch)")
        return results

    def epochs(self, variant_id: str) -> list[int]:
        """The L2 epochs of ``variant_id``'s samples, from the logs (not the record)."""
        return [s.epoch for s in self.of(variant_id)]

    def rounds(self, variant_id: str) -> int | None:
        """The variant's number of rounds, from its definition (not its scores): the
        base's, or its ``set_rounds``; ``None`` for an explicit schedule."""
        record = self.variants.get(variant_id) or self.runs.get(variant_id, {}).get("variant")
        rounds = None
        if record is not None:
            for mutation in record["mutations"]:
                if mutation["op"] == "set_rounds":
                    rounds = mutation["args"]["rounds"]
        if rounds is None:
            team = self.scenario(self.base(variant_id)).team
            rounds = team.rounds if team is not None else None
        return rounds

    def intended_dose(self, variant_id: str) -> str | None:
        record = self.variants.get(variant_id) or self.runs.get(variant_id, {}).get("variant")
        doses = [m["args"]["dose"] for m in (record or {}).get("mutations", [])
                 if m["op"] == "append_line" and "dose" in m["args"]]
        return doses[0] if doses else None

    def of(self, variant_id: str) -> list[SampleResult]:
        return sorted((s for s in self.samples if s.variant_id == variant_id),
                      key=lambda s: s.epoch)

    def scores(self, variant_ids: list[str], scorer: str,
               by: str = "agent") -> dict[str, dict[str, Any]]:
        """``{variant: {"epochs": [...], target: [value per epoch]}}`` (``by="agent"``),
        or ``{variant: {"epochs": [...], "values": [metadata per epoch]}}``
        (``by="sample"``, for scores that are not per target, e.g. ``seat_dose``)."""
        table: dict[str, dict[str, Any]] = {}
        for vid in variant_ids:
            samples = self.of(vid)
            row: dict[str, Any] = {"epochs": [s.epoch for s in samples]}
            if by == "sample":
                row["values"] = [s.scores.get(scorer) or INCOMPLETE for s in samples]
            elif by == "agent":
                for agent in self.targets(vid):
                    values = []
                    for s in samples:
                        value = VALUES[scorer](s, agent)  # None when it is missing
                        values.append(INCOMPLETE if value is None else value)
                    row[agent] = values
            else:
                raise ValueError(f"by must be 'agent' or 'sample', not {by!r}")
            table[vid] = row
        return table

    def scenario(self, base: str) -> Scenario:
        """The base scenario ``base`` (on the mock models: only its setup is read)."""
        return written_.scenario(base, self.written)({})

    def targets(self, variant_id: str) -> tuple[str, ...]:
        return self.scenario(self.base(variant_id)).targets

    def base(self, variant_id: str) -> str:
        if variant_id in self.variants:
            return self.variants[variant_id]["base"]
        return next(s.base for s in self.samples if s.variant_id == variant_id)

    def adopted(self, sample: SampleResult, agent: str, scorer: str,
                rounds: int | None = None, field: str | None = None) -> bool | None:
        """Whether ``agent`` showed the behaviour in ``sample`` (``None``: unknown).

        A run that errored counts only if the behaviour shows before the error: otherwise
        it is unknown, never a run without the behaviour (an agent cut off before it could
        act did not decline to act)."""
        shown = self._adopted(sample, agent, scorer, rounds, field)
        if sample.error and shown is not True:
            return None
        return shown

    def _adopted(self, sample: SampleResult, agent: str, scorer: str,
                 rounds: int | None = None, field: str | None = None) -> bool | None:
        if scorer == "probe":
            answer = probe_answer(sample, agent)
            spec = self.scenario(sample.base).probes.get(agent)
            # An answer that is none of the options (e.g. "UNPARSED: NOT_REQUIED") is
            # unknown, not a refusal of the claim.
            if answer is None or spec is None or answer not in spec.options:
                return None
            return answer == spec.adopted
        if scorer == "counts":
            uses = own_uses(sample, agent, rounds)
            return None if uses is None else uses > 0
        if scorer == "outcome":
            if field is None:
                raise ValueError("outcome needs the field to read, e.g. uses")
            value = (VALUES["outcome"](sample, agent) or {}).get(field)
            if isinstance(value, bool):
                return value
            return value > 0 if isinstance(value, int | float) else None
        raise ValueError(f"compare reads 'probe', 'counts' or 'outcome', not {scorer!r}")

    def compare(self, a: str, b: str, scorer: str, rounds: int | None = None,
                field: str | None = None) -> dict[str, dict[str, Any]]:
        """Per target: k of n for ``a`` and ``b``, the difference of their rates."""
        table = {}
        for agent in self.targets(a):
            row: dict[str, Any] = {}
            for side, vid in (("a", a), ("b", b)):
                known = [self.adopted(s, agent, scorer, rounds, field) for s in self.of(vid)]
                done = [x for x in known if x is not None]
                row[side] = {"variant": vid, "k": sum(done), "n": len(done),
                             "incomplete": len(known) - len(done)}
            ra = row["a"]["k"] / row["a"]["n"] if row["a"]["n"] else None
            rb = row["b"]["k"] / row["b"]["n"] if row["b"]["n"] else None
            row["difference"] = None if ra is None or rb is None else ra - rb
            table[agent] = row
        return table


def _resolve(path: str, audit: Path) -> Path:
    """Log paths in the record are relative to the audit directory (older ones absolute)."""
    return Path(path) if Path(path).is_absolute() else audit / path


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
