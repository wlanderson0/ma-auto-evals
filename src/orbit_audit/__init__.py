"""Petri-style automated auditing for multi-agent evaluations on Orbit.

Orbit owns the multi-agent runtime (agents, schedules, channels); Inspect owns
models, tools, samples and logs. ``orbit_audit`` adds an auditor control plane
on top: typed control operations at lifecycle boundaries, append-only
intervention evidence, state projections, and branching by deterministic
re-execution. The LLM auditor is one client of that API; researchers can
script audits against it directly.

See ``docs/spec.md`` for the specification and ``docs/auditor-tests.md`` for
the test contract.
"""

__version__ = "0.0.1"
