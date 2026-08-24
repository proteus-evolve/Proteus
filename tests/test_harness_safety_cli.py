from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from proteus import cli
from proteus.adapters.minimal import MinimalHarness
from proteus.safety.phase1 import SUITE
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.taxonomy import HarnessModule


class _NoopExecutor:
    name = "cli-noop"

    def collect(self, definition, endpoint, arm, stratum, context, channel):
        del definition, endpoint, arm, stratum, context, channel
        raise AssertionError("preflight test must not execute a safety cell")


class _CandidateAdapter(MinimalHarness):
    name = "cli-candidate"

    def candidate_safety_executor(self):
        return _NoopExecutor()


def _install_candidate_adapter(monkeypatch: pytest.MonkeyPatch) -> str:
    module = types.ModuleType("fixture_candidate_adapter")
    module.CandidateAdapter = _CandidateAdapter
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return f"{module.__name__}:CandidateAdapter"


def _base_run(out: Path, *, harness: str = "aki") -> list[str]:
    return [
        "run",
        "--harness",
        harness,
        "--arm",
        "neutral",
        "--goal",
        "none",
        "--seeds",
        "1",
        "--episodes",
        "1",
        "--out",
        str(out),
    ]


def test_aki_adapter_declares_all_canonical_module_bindings() -> None:
    from proteus.adapters.aki import AkiHarness

    adapter = AkiHarness()
    profile = adapter.harness_safety_profile()

    profile.validate_surfaces(adapter.surfaces())
    assert {binding.module for binding in profile.bindings} == set(HarnessModule)


def test_repository_root_follows_a_linked_worktree_to_the_common_checkout(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    git_dir = repository / ".git/worktrees/safety"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n")

    assert cli._repository_root(worktree) == repository


def test_run_preflight_rejects_missing_live_credential_before_output_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    out = tmp_path / "never-created"
    monkeypatch.setattr(cli, "_repository_root", lambda: repository)

    code = cli.main(
        [
            *_base_run(out),
            "--model",
            "gpt-5.6-luna",
            "--safety-suite",
            "proteus.safety.phase1:SUITE",
        ]
    )

    assert code == 2
    assert "repository-root credential file is missing" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize(
    ("families", "message"),
    [
        (("memory_collapse", "memory_collapse"), "duplicate safety family"),
        (("unknown-family",), "unknown safety family"),
    ],
)
def test_run_preflight_rejects_duplicate_or_unknown_families_before_output(
    tmp_path: Path,
    families: tuple[str, ...],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "never-created"
    argv = [
        *_base_run(out),
        "--model",
        "gpt-5.6-luna",
        "--safety-suite",
        "proteus.safety.phase1:SUITE",
    ]
    for family in families:
        argv.extend(("--safety-family", family))

    assert cli.main(argv) == 2
    assert message in capsys.readouterr().err
    assert not out.exists()


def test_run_preflight_rejects_adapter_without_candidate_safety_protocol(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "never-created"

    code = cli.main(
        [
            *_base_run(out, harness="minimal"),
            "--safety-suite",
            "proteus.safety.phase1:SUITE",
            "--safety-family",
            "memory_collapse",
        ]
    )

    assert code == 2
    assert "does not implement candidate safety" in capsys.readouterr().err
    assert not out.exists()


def test_run_preflight_constructs_per_run_gate_with_selected_definitions_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _install_candidate_adapter(monkeypatch)
    captured = []

    def fake_run_sweep(config):
        captured.append(config)
        return []

    monkeypatch.setattr(cli, "run_sweep", fake_run_sweep)
    out = tmp_path / "sweep"

    code = cli.main(
        [
            *_base_run(out, harness=harness),
            "--safety-suite",
            "proteus.safety.phase1:SUITE",
            "--safety-family",
            "memory_collapse",
        ]
    )

    assert code == 0
    assert len(captured) == 1
    gate = captured[0].candidate_gate_factory("run-1")
    assert [item.family_id for item in gate.suite.definitions()] == ["memory_collapse"]
    assert gate.controller_root == out
    assert gate.model_config is None
    assert gate.broker is None


def test_safety_family_without_suite_is_rejected_before_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "never-created"

    code = cli.main([*_base_run(out), "--safety-family", "memory_collapse"])

    assert code == 2
    assert "--safety-family requires --safety-suite" in capsys.readouterr().err
    assert not out.exists()


def test_completed_sweep_safety_command_is_removed(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["safety", "--help"])

    assert caught.value.code == 2
    assert "invalid choice: 'safety'" in capsys.readouterr().err


def test_run_help_exposes_only_suite_and_repeatable_family_safety_controls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["run", "--help"])

    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "--safety-suite" in output
    assert "--safety-family" in output
    assert "feedback" not in output.lower()
    assert "threshold" not in output.lower()
    assert "policy" not in output.lower()


def test_phase1_suite_is_still_definitions_only() -> None:
    assert SUITE.definitions()
    assert not callable(getattr(SUITE, "provider", None))
    assert CandidateSafetyContext.__module__ == "proteus.safety.plugins"
