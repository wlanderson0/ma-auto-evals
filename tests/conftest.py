"""Shared test policy: offline by default, mirroring Orbit's own test suite.

Ordinary tests may only generate with ``mockllm/`` models; sockets are blocked
by pytest-socket (see ``addopts``). Real-provider tests are marked ``live`` and
need both ``--run-live`` and ``-m live``; they may cost money.
"""

from __future__ import annotations

import os
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Allow explicitly marked live tests to use credentials and network (may cost money).",
    )


def pytest_configure(config: pytest.Config) -> None:
    # Before collection: Inspect initialises its platform on import and must not
    # read a developer's .env. macOS temp paths can exceed AF_UNIX limits once
    # Inspect adds control-socket directories, so prefer /tmp.
    scratch = TemporaryDirectory(
        prefix="orbit-audit-pytest-", dir="/tmp" if os.name == "posix" else None
    )
    config._orbit_audit_scratch = scratch  # type: ignore[attr-defined]
    scratch_path = Path(scratch.name)
    values = {
        "ORBIT_AUTOFETCH": "0",
        "ORBIT_AUTOSTART_SERVICE": "0",
        "XDG_CACHE_HOME": str(scratch_path / "cache"),
        "XDG_DATA_HOME": str(scratch_path / "data"),
        "INSPECT_TRACE_FILE": str(scratch_path / "inspect-trace.log"),
    }
    if not config.getoption("--run-live"):
        values["PYTHON_DOTENV_DISABLED"] = "1"
    config._orbit_audit_original_env = {  # type: ignore[attr-defined]
        name: os.environ.get(name) for name in values
    }
    os.environ.update(values)


def pytest_unconfigure(config: pytest.Config) -> None:
    for name, value in getattr(config, "_orbit_audit_original_env", {}).items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    scratch = getattr(config, "_orbit_audit_scratch", None)
    if scratch is not None:
        scratch.cleanup()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-live"):
        skip = pytest.mark.skip(
            reason="Live tests require both --run-live and explicit selection (-m live)."
        )
        for item in items:
            if item.get_closest_marker("live") is not None:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def scratch_dir(request: pytest.FixtureRequest) -> Path:
    """Session scratch directory (short path; safe for Inspect logs)."""
    return Path(request.config._orbit_audit_scratch.name)  # type: ignore[attr-defined]


def _offline_patches(mp: pytest.MonkeyPatch) -> None:
    """Patches that keep Inspect deterministic and local without a network."""
    from inspect_ai.model import Model
    from inspect_ai.model._providers.mockllm import MockLLM

    # MockLLM otherwise lazily downloads a tiktoken vocabulary on first use.
    async def mock_text_tokens(self, text: str) -> int:
        return (len(text) + 3) // 4

    mp.setattr(MockLLM, "count_text_tokens", mock_text_tokens)

    original_generate = Model.generate

    @wraps(original_generate)
    async def offline_generate(model: Model, *args, **kwargs):
        if not str(model).startswith("mockllm/"):
            raise RuntimeError(
                "Non-mock model generation is disabled in offline tests. Use mockllm, "
                "or mark the test live and opt in with --run-live -m live."
            )
        return await original_generate(model, *args, **kwargs)

    mp.setattr(Model, "generate", offline_generate)


@pytest.fixture(autouse=True)
def offline_test_policy(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Protect ordinary tests even when the shell already holds API keys."""
    if request.node.get_closest_marker("live") is not None:
        if not request.config.getoption("--run-live"):
            pytest.skip("Live tests require --run-live.")
        request.getfixturevalue("socket_enabled")
        return
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    _offline_patches(monkeypatch)


@pytest.fixture(scope="module")
def offline_module():
    """Module-scoped equivalent of the offline policy, for expensive shared runs.

    Module fixtures are set up before function-scoped autouse fixtures, so a
    module-level ``inspect_eval`` needs its own patches.
    """
    with pytest.MonkeyPatch.context() as mp:
        _offline_patches(mp)
        yield
