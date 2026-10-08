"""Install-level smoke tests: the package and its pinned runtime import together."""

from importlib.metadata import version


def test_package_imports_with_version():
    import orbit_audit

    assert orbit_audit.__version__ == version("orbit-audit")


def test_orbit_and_inspect_are_the_pinned_baseline():
    # Raise these together with the Orbit pin in pyproject.toml.
    assert version("inspect-ai") == "0.3.263"
    assert version("orbit") == "1.0.3"
