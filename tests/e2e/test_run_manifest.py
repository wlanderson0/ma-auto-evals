"""``<out>/manifest.json``: what ``cosmos upload`` files a run under.

Written at the start of ``experiments.run`` and refreshed with ``finished_at`` at
the end; git lookups fall back to nulls so they can never fail a run.
"""

from __future__ import annotations

import json
import re

import pytest

from experiments.run import CONDITIONS, MANIFEST_SCHEMA, REPO, build_manifest, main, redact_argv
from experiments.scenarios import SCENARIOS

GIT_NULLS = {"sha": None, "short_sha": None, "branch": None, "dirty": None}


@pytest.fixture(autouse=True)
def no_pr_env(monkeypatch):
    monkeypatch.delenv("COSMOS_PR", raising=False)
    monkeypatch.delenv("GITHUB_PR", raising=False)


def test_mock_run_writes_and_finishes_the_manifest(tmp_path):
    argv = ["--mock", "--scenario", "mfa_rumor", "--conditions", "baseline", "--epochs", "1",
            "--out", str(tmp_path), "--pr", "22"]
    main(argv)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["schema"] == MANIFEST_SCHEMA
    assert manifest["pr"] == 22
    assert (manifest["scenarios"], manifest["conditions"], manifest["epochs"]) == (
        ["mfa_rumor"], ["baseline"], 1)
    assert manifest["argv"] == [*argv[:-3], f".../{tmp_path.name}", "--pr", "22"]
    assert str(tmp_path) not in json.dumps(manifest)
    assert manifest["seats"] == {"mfa_rumor": SCENARIOS["mfa_rumor"]({}).seat}
    rows = json.loads((tmp_path / "results.json").read_text())["results"]
    assert {row["seat"] for row in rows} == {manifest["seats"]["mfa_rumor"]}
    assert manifest["started_at"] <= manifest["finished_at"]
    models = manifest["models"]
    assert {role: models[role] for role in ("target", "auditor", "judge", "probe")} == {
        "target": "mockllm/model", "auditor": "mockllm/auditor",
        "judge": "mockllm/judge", "probe": "mockllm/judge"}
    assert models["agents"] == {"mfa_rumor": dict.fromkeys(
        ("alice", "bob", "carol", "dave"), "mockllm/model")}
    # The test suite runs from the ma-auto-evals checkout.
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["sha"])
    assert manifest["short_sha"] == manifest["sha"][:7]
    assert isinstance(manifest["dirty"], bool)
    assert (tmp_path / "results.json").exists()


def test_no_manifest_flag_leaves_the_parents_manifest_alone(tmp_path):
    (tmp_path / "manifest.json").write_text('{"owner": "parent"}')
    main(["--mock", "--scenario", "mfa_rumor", "--conditions", "baseline", "--epochs", "1",
          "--out", str(tmp_path), "--no-manifest", "--results-name", "results-part.json"])
    assert json.loads((tmp_path / "manifest.json").read_text()) == {"owner": "parent"}


def test_live_models_are_resolved_per_role_and_agent():
    from experiments.run import AUDITOR, JUDGE, TARGETS

    models = {"target": AUDITOR, "auditor": AUDITOR, "judge": JUDGE}
    scenarios = {name: SCENARIOS[name](TARGETS[name]) for name in ("release_pressure", "mfa_rumor")}
    manifest = build_manifest(scenarios, list(CONDITIONS), 2, models, pr=None, argv=[])
    assert manifest["models"] == {**models, "agents": {name: TARGETS[name] for name in scenarios}}
    assert manifest["finished_at"] is None


def test_outside_a_git_checkout_git_fields_are_null(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    manifest = build_manifest({}, ["l1"], 1, {"target": "mockllm/model"}, pr=None, argv=[],
                              cwd=tmp_path)
    assert {key: manifest[key] for key in GIT_NULLS} == GIT_NULLS


@pytest.mark.parametrize("cwd", ["missing-dir", "."])
def test_missing_git_or_directory_never_crashes(tmp_path, monkeypatch, cwd):
    monkeypatch.setenv("PATH", str(tmp_path))  # no git executable on PATH
    manifest = build_manifest({}, ["l1"], 1, {"target": "mockllm/model"}, pr=None, argv=[],
                              cwd=tmp_path / cwd)
    assert {key: manifest[key] for key in GIT_NULLS} == GIT_NULLS


@pytest.mark.parametrize("flag, env, expected", [
    (None, {}, None),
    (None, {"GITHUB_PR": "7"}, 7),
    (None, {"COSMOS_PR": "9", "GITHUB_PR": "7"}, 9),
    (None, {"COSMOS_PR": "not-a-pr", "GITHUB_PR": "7"}, 7),
    (3, {"COSMOS_PR": "9", "GITHUB_PR": "7"}, 3),
])
def test_pr_comes_from_the_flag_then_the_environment(monkeypatch, flag, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    manifest = build_manifest({}, ["l1"], 1, {"target": "mockllm/model"}, pr=flag, argv=[])
    assert manifest["pr"] == expected


def test_argv_paths_are_redacted():
    argv = ["--out", "/home/someone/runs/x", f"--out={REPO / 'runs' / 'y'}", "--scenario", "all",
            str(REPO)]
    assert redact_argv(argv) == ["--out", ".../x", "--out=runs/y", "--scenario", "all", "."]
