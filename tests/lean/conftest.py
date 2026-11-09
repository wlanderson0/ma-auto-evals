from __future__ import annotations

import os

import pytest

from tests.lean.driver import LeanDriver, lean_binary


@pytest.fixture(scope="session")
def lean():
    """The Lean model, as one driver process shared by the session."""
    binary = lean_binary()
    if binary is None:
        if os.environ.get("LEAN_REQUIRED"):
            pytest.fail("LEAN_REQUIRED is set but there is no lean binary or built model")
        pytest.skip("no lean binary or built model (set LEAN; build with lean/build.sh)")
    driver = LeanDriver(binary)
    yield driver
    driver.close()
