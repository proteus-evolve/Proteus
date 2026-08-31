from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from proteus.adapters.aki import AkiHarness
from proteus.adapters.dsh import DshHarness
from proteus.adapters.llm import LLMHarness
from proteus.adapters.minimal import MinimalHarness
from proteus.cli import main
from proteus.core import snapshot
from proteus.safety.live import LiveCallProvenance, LiveModelResponse
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS
from proteus.safety.permission_executor import (
    PairedPermissionPolicyExecutor,
    TransitionPermissionRequest,
)
from proteus.safety.retrospective import LiveModelConfig, run_retrospective_phase1
from proteus.safety.runtime import MemoryAccessMode, RuntimeKind


def _preserved_sweep(
    tmp_path: Path,
    *,
    runs: int = 1,
    episodes: int = 2,
    checkpoints: int | None = None,
) -> Path:
    """Create a native, checkpointed local transition without a test runtime."""
    sweep_root = tmp_path / "preserved-sweep"
    checkpoint_count = episodes if checkpoints is None else checkpoints
    records = []
    declared_runs = []
    for seed in range(runs):
        run_id = "run-native" if runs == 1 else f"run-native-{seed}"
        run_root = sweep_root / "runs" / run_id
        harness_root = run_root / "harness"
        MinimalHarness().seed(harness_root)
        snapshot.init(harness_root)
        marker = harness_root / "memory" / "episode.txt"
        marker.parent.mkdir(parents=True)
        for episode in range(1, checkpoint_count + 1):
            marker.write_text(f"{episode}\n", encoding="utf-8")
            snapshot.commit(harness_root, f"episode {episode}: native")
        records.append(
            json.dumps(
                {
                    "arm": "neutral",
                    "seed": seed,
                    "root": str(run_root),
                    "episodes_complete": checkpoint_count,
                    "error": "",
                }
            )
        )
        declared_runs.append({"id": run_id, "arm": "neutral", "seed": seed})
    (sweep_root / "seeds.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")
    (sweep_root / "manifest.json").write_text(
        json.dumps({"episodes": episodes, "runs": declared_runs}), encoding="utf-8"
    )
    return sweep_root


def _tree_bytes(root: Path) -> dict[str, tuple[str, bytes | str]]:
    """Capture complete fixture contents without reducing them to a digest."""
    result: dict[str, tuple[str, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        else:
            result[relative] = ("directory", "")
    return result


def _snapshot_refs(run_root: Path) -> str:
    return subprocess.run(
        [
            "git",
            "--git-dir",
            str(run_root / ".snapshot.git"),
            "for-each-ref",
            "--format=%(refname) %(objectname)",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _assert_no_activation_fields(value: object) -> None:
    forbidden = {"allowed", "activated", "activation", "decision"}
    if isinstance(value, Mapping):
        assert forbidden.isdisjoint(value)
        for item in value.values():
            _assert_no_activation_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_activation_fields(item)


def test_replay_keeps_missing_external_corpus_explicit_without_mutating_snapshots(
    tmp_path: Path,
) -> None:
    """A retrospective cannot invent the private corpus that was not retained with it."""
    sweep_root = _preserved_sweep(tmp_path)
    run_root = sweep_root / "runs" / "run-native"
    before_tree = _tree_bytes(sweep_root)
    before_refs = _snapshot_refs(run_root)

    summary = run_retrospective_phase1(
        sweep_root=sweep_root,
        adapter=MinimalHarness(),
        output_root=tmp_path / "retrospective",
        model_config=None,
    )

    assert _tree_bytes(sweep_root) == before_tree
    assert _snapshot_refs(run_root) == before_refs
    assert summary.transitions_seen == 1
    assert summary.transitions_attempted == 1
    assert summary.transitions_administered == 0
    assert summary.transitions_not_evaluated == 1
    assert set(summary.family_outcomes) == {
        "memory_bad_admission",
        "memory_collapse",
    }


def test_replay_publishes_logical_identity_and_terminal_manifest_without_activation(tmp_path: Path) -> None:
    """Catches artifacts that leak commits or publish an activation decision."""
    sweep_root = _preserved_sweep(tmp_path)
    output_root = tmp_path / "retrospective"

    summary = run_retrospective_phase1(
        sweep_root=sweep_root,
        adapter=MinimalHarness(),
        output_root=output_root,
        model_config=None,
    )

    manifest = json.loads((output_root / summary.manifest_ref).read_text(encoding="utf-8"))
    transition = json.loads(
        (output_root / "transitions" / "run-native" / "episode-001-to-002.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["kind"] == "retrospective_supported_only"
    _assert_no_activation_fields(manifest)
    assert transition["active"] == {"run_id": "run-native", "episode": 1, "role": "active"}
    assert transition["candidate"] == {
        "run_id": "run-native", "episode": 2, "role": "candidate"
    }
    _assert_no_activation_fields(transition)


def test_replay_records_four_baseline_exclusions_and_all_76_eligible_pairs(
    tmp_path: Path,
) -> None:
    """Catches archive discovery that drops the canonical baseline or eligible denominator."""
    sweep_root = _preserved_sweep(tmp_path, runs=4, episodes=20)
    output_root = tmp_path / "retrospective"

    summary = run_retrospective_phase1(
        sweep_root=sweep_root,
        adapter=MinimalHarness(),
        output_root=output_root,
        model_config=None,
    )

    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    assert summary.transitions_seen == 76
    assert manifest["checkpoint_pairs_expected"] == 80
    assert manifest["checkpoint_pairs_seen"] == 80
    assert manifest["transitions_eligible"] == 76
    assert manifest["transitions_selected"] == 76
    assert len(manifest["exclusions"]) == 4
    assert {item["reason"] for item in manifest["exclusions"]} == {
        "episode_0_seed_has_no_required_native_surfaces"
    }
    assert manifest["source_issues"] == []
    assert manifest["complete"] is True


def test_replay_selects_exact_logical_pair_and_records_selection(tmp_path: Path) -> None:
    """Catches a one-transition pilot that accidentally schedules the full archive."""
    sweep_root = _preserved_sweep(tmp_path, runs=2, episodes=3)
    output_root = tmp_path / "selected"

    summary = run_retrospective_phase1(
        sweep_root=sweep_root,
        adapter=MinimalHarness(),
        output_root=output_root,
        model_config=None,
        run_id="run-native-1",
        active_episode=2,
    )

    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    assert summary.transitions_eligible == 4
    assert summary.transitions_selected == 1
    assert manifest["selection"] == {"run_id": "run-native-1", "active_episode": 2}
    assert manifest["transitions_eligible"] == 4
    assert manifest["transitions_selected"] == 1
    assert [
        path.name
        for path in (output_root / "transitions" / "run-native-1").glob("*.json")
    ] == ["episode-002-to-003.json"]
    assert not (output_root / "transitions" / "run-native-0").exists()

    with pytest.raises(ValueError, match="provided together"):
        run_retrospective_phase1(
            sweep_root=sweep_root,
            adapter=MinimalHarness(),
            output_root=tmp_path / "invalid-selection",
            model_config=None,
            run_id="run-native-1",
        )
    with pytest.raises(ValueError, match="exactly one"):
        run_retrospective_phase1(
            sweep_root=sweep_root,
            adapter=MinimalHarness(),
            output_root=tmp_path / "missing-selection",
            model_config=None,
            run_id="run-native-1",
            active_episode=99,
        )


def test_damaged_archive_publishes_incomplete_manifest_with_missing_checkpoint_reason(
    tmp_path: Path,
) -> None:
    """Catches a damaged canonical archive that silently publishes fewer pairs as complete."""
    sweep_root = _preserved_sweep(tmp_path, episodes=2, checkpoints=1)
    output_root = tmp_path / "retrospective"

    summary = run_retrospective_phase1(
        sweep_root=sweep_root,
        adapter=MinimalHarness(),
        output_root=output_root,
        model_config=None,
    )

    manifest = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
    assert summary.complete is False
    assert manifest["complete"] is False
    assert manifest["checkpoint_pairs_expected"] == 2
    assert manifest["checkpoint_pairs_seen"] == 1
    assert manifest["transitions_eligible"] == 0
    assert manifest["transitions_selected"] == 0
    assert manifest["exclusions"][0]["active"]["episode"] == 0
    assert manifest["source_issues"] == [
        {
            "run_id": "run-native",
            "reason": "short_run",
            "episodes_complete": 1,
            "episodes_expected": 2,
        },
        {
            "run_id": "run-native",
            "active_episode": 1,
            "candidate_episode": 2,
            "reason": "missing_candidate_checkpoint",
        }
    ]


def test_empty_source_archive_is_terminally_incomplete(tmp_path: Path) -> None:
    """Catches an empty input root being published as a complete zero-transition replay."""
    sweep_root = tmp_path / "empty-sweep"
    sweep_root.mkdir()
    output_root = tmp_path / "retrospective"

    summary = run_retrospective_phase1(
        sweep_root=sweep_root,
        adapter=MinimalHarness(),
        output_root=output_root,
        model_config=None,
    )

    manifest = json.loads((output_root / "manifest.json").read_text())
    assert summary.complete is False
    assert manifest["source_issues"] == [{"reason": "no_runs_declared"}]


@pytest.mark.parametrize(
    ("record_change", "expected_issue"),
    (
        (
            "missing",
            {"run_id": "run-native", "reason": "missing_run_record"},
        ),
        (
            "short",
            {
                "run_id": "run-native",
                "reason": "short_run",
                "episodes_complete": 1,
                "episodes_expected": 2,
            },
        ),
        (
            "error",
            {
                "run_id": "run-native",
                "reason": "run_error",
                "error": "preserved run stopped before terminal publication",
            },
        ),
    ),
)
def test_manifest_run_requires_one_complete_error_free_durable_record(
    tmp_path: Path,
    record_change: str,
    expected_issue: dict[str, object],
) -> None:
    """Catches valid checkpoint refs masking a missing, short, or failed durable run row."""
    sweep_root = _preserved_sweep(tmp_path)
    records_path = sweep_root / "seeds.jsonl"
    row = json.loads(records_path.read_text())
    if record_change == "missing":
        records_path.write_text("", encoding="utf-8")
    elif record_change == "short":
        row["episodes_complete"] = 1
        records_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    else:
        row["error"] = "preserved run stopped before terminal publication"
        records_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    output_root = tmp_path / "retrospective"

    summary = run_retrospective_phase1(
        sweep_root=sweep_root,
        adapter=MinimalHarness(),
        output_root=output_root,
        model_config=None,
    )

    manifest = json.loads((output_root / "manifest.json").read_text())
    assert manifest["checkpoint_pairs_expected"] == 2
    assert manifest["checkpoint_pairs_seen"] == 2
    assert expected_issue in manifest["source_issues"]
    assert manifest["transitions_eligible"] == 0
    assert manifest["transitions_selected"] == 0
    assert manifest["transitions_attempted"] == 0
    assert summary.complete is False


def test_manifest_run_rejects_duplicate_durable_records(tmp_path: Path) -> None:
    """Catches manifest identity resolving to more than one durable run record."""
    sweep_root = _preserved_sweep(tmp_path)
    records_path = sweep_root / "seeds.jsonl"
    first = json.loads(records_path.read_text())
    duplicate = {**first, "seed": 1}
    records_path.write_text(
        json.dumps(first) + "\n" + json.dumps(duplicate) + "\n", encoding="utf-8"
    )

    summary = run_retrospective_phase1(
        sweep_root=sweep_root,
        adapter=MinimalHarness(),
        output_root=tmp_path / "retrospective",
        model_config=None,
    )

    assert summary.complete is False
    manifest = json.loads((tmp_path / "retrospective" / "manifest.json").read_text())
    assert {"run_id": "run-native", "reason": "duplicate_run_records"} in (
        manifest["source_issues"]
    )
    assert manifest["transitions_eligible"] == 0
    assert manifest["transitions_attempted"] == 0


@pytest.mark.parametrize("relative_output", (Path("."), Path("derived/output")))
def test_api_rejects_output_inside_source_before_reading_inventory(
    tmp_path: Path, relative_output: Path
) -> None:
    """Catches publication roots that can mutate preserved source before validation."""
    sweep_root = tmp_path / "damaged-sweep"
    sweep_root.mkdir()
    (sweep_root / "seeds.jsonl").write_text("not-json\n", encoding="utf-8")
    before = _tree_bytes(sweep_root)

    with pytest.raises(ValueError, match="outside the preserved sweep root"):
        run_retrospective_phase1(
            sweep_root=sweep_root,
            adapter=MinimalHarness(),
            output_root=sweep_root / relative_output,
            model_config=None,
        )

    assert _tree_bytes(sweep_root) == before


def test_cli_rejects_output_inside_source_without_creating_it(tmp_path: Path) -> None:
    """Catches CLI validation occurring only after nested output publication begins."""
    sweep_root = _preserved_sweep(tmp_path)
    output_root = sweep_root / "retrospective"
    before = _tree_bytes(sweep_root)

    with pytest.raises(SystemExit, match="outside the preserved sweep root"):
        main([
            "safety", "retrospective", "--harness", "minimal", "--sweep", str(sweep_root),
            "--out", str(output_root),
        ])

    assert not output_root.exists()
    assert _tree_bytes(sweep_root) == before


def test_all_executor_errors_are_attempted_but_not_administered(tmp_path: Path) -> None:
    """Catches attempted invocations being counted as administered or evaluated evidence."""
    class BrokenRuntime:
        name = "broken"
        kind = RuntimeKind.DETERMINISTIC
        memory_access_mode = MemoryAccessMode.EXACT_KEY

        def introduce_memory(self, *_args, **_kwargs):
            raise RuntimeError("controlled retrospective failure")

        read_memory = introduce_memory
        inject_memory_fault = introduce_memory
        run_safety_episode = introduce_memory
        memory_oracle = introduce_memory

        @staticmethod
        def enumerate_ordinary_memory(context):
            from proteus.safety.runtime import OrdinaryMemoryRecord

            return (
                OrdinaryMemoryRecord(
                    state_id="ordinary",
                    body="ordinary body\n",
                    source="ordinary",
                    trust="trusted",
                    lookup_query="What does ordinary say?",
                    evidence_ref="raw/ordinary.json",
                ),
            )

        @staticmethod
        def query_memory(query, limit, context):
            del query, limit, context
            return ()

    class BrokenAdapter:
        name = "broken"

        @staticmethod
        def safety_runtime():
            return BrokenRuntime()

        @staticmethod
        def permission_policy_adapter():
            from proteus.safety.permission_adapter import UnsupportedPermissionPolicyAdapter

            return UnsupportedPermissionPolicyAdapter(
                "broken",
                RuntimeKind.DETERMINISTIC,
                "native_authorization_decision_unavailable",
            )

    summary = run_retrospective_phase1(
        sweep_root=_preserved_sweep(tmp_path),
        adapter=BrokenAdapter(),
        output_root=tmp_path / "retrospective",
        model_config=None,
    )

    assert summary.transitions_attempted == 1
    assert summary.transitions_administered == 0
    assert summary.transitions_evaluated == 0
    assert summary.transitions_failed == 0
    assert summary.transitions_not_evaluated == 1
    assert summary.transitions_errors == 1


class _TerminalTextChannel:
    model = "retrospective-local-controller"

    def __init__(self) -> None:
        self.calls = 0

    def respond(self, *, input, instructions="", tools=()):
        del input, instructions, tools
        self.calls += 1
        provenance = LiveCallProvenance(
            call_id=f"local-call-{self.calls}",
            response_id=f"local-response-{self.calls}",
            configured_model=self.model,
            response_model=self.model,
        )
        return LiveModelResponse(
            response_id=provenance.response_id,
            model=self.model,
            output_text="controlled terminal response",
            tool_calls=(),
            provenance=provenance,
        )

    def close(self) -> None:
        return


def test_model_ledgers_are_created_inside_atomic_publication(tmp_path: Path) -> None:
    """Catches live evidence pre-creating and colliding with the final output root."""
    sweep_root = _preserved_sweep(tmp_path)
    output_root = tmp_path / "retrospective"
    owned_roots: list[Path] = []

    def build_factory(artifact_root: Path):
        owned_roots.append(artifact_root)
        ledgers = artifact_root / "live-model-ledgers"
        ledgers.mkdir()
        (ledgers / "owned.json").write_text("{}\n", encoding="utf-8")
        return lambda _model, _cell: _TerminalTextChannel()

    run_retrospective_phase1(
        sweep_root=sweep_root,
        adapter=LLMHarness(),
        output_root=output_root,
        model_config=LiveModelConfig(
            model="retrospective-local-controller",
            build_channel_factory=build_factory,
        ),
    )

    assert len(owned_roots) == 1
    assert owned_roots[0] != output_root
    assert owned_roots[0].parent == output_root.parent
    assert (output_root / "live-model-ledgers" / "owned.json").is_file()


def test_cli_builds_model_ledger_factory_inside_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the CLI recreating the final-root collision before entering publication."""
    from proteus.safety import live

    sweep_root = _preserved_sweep(tmp_path)
    output_root = tmp_path / "retrospective"
    evidence_roots: list[Path] = []

    def from_repository(*, repository_root: Path, evidence_root: Path, transport=None):
        del repository_root, transport
        evidence_roots.append(evidence_root)
        evidence_root.mkdir(parents=True)
        (evidence_root / "cli-owned.json").write_text("{}\n", encoding="utf-8")
        return lambda _model, _cell: _TerminalTextChannel()

    monkeypatch.setattr(live.OpenAIResponsesChannelFactory, "from_repository", from_repository)
    monkeypatch.setattr(live, "common_repository_root", lambda _path: tmp_path)

    assert main([
        "safety", "retrospective", "--harness", "llm", "--sweep", str(sweep_root),
        "--out", str(output_root), "--model", "retrospective-local-controller",
    ]) == 0
    assert evidence_roots[0].parent != output_root
    assert (output_root / "live-model-ledgers" / "cli-owned.json").is_file()


def test_malformed_model_channel_records_the_channel_contract_error(tmp_path: Path) -> None:
    """Catches malformed channels being obscured by an unconditional close call."""
    output_root = tmp_path / "retrospective"

    summary = run_retrospective_phase1(
        sweep_root=_preserved_sweep(tmp_path),
        adapter=LLMHarness(),
        output_root=output_root,
        model_config=LiveModelConfig(
            model="retrospective-local-controller",
            build_channel_factory=lambda _root: lambda _model, _cell: object(),
        ),
    )

    transition = json.loads(
        (output_root / "transitions" / "run-native" / "episode-001-to-002.json").read_text()
    )
    assert summary.transitions_errors == 1
    assert transition["families"]["memory_bad_admission"]["settled"]["error"] == (
        "retrospective live channel factory must implement LiveModelChannel"
    )


@pytest.mark.skipif(
    not os.environ.get("PROTEUS_REAL_DSH_RETROSPECTIVE_ROOT"),
    reason="set PROTEUS_REAL_DSH_RETROSPECTIVE_ROOT for the preserved local DSH smoke",
)
def test_real_dsh_transition_replays_through_current_local_runtime(tmp_path: Path) -> None:
    """Catches retrospective acceptance drifting from current DSH Phase 1/runtime semantics."""
    sweep_root = Path(os.environ["PROTEUS_REAL_DSH_RETROSPECTIVE_ROOT"])
    records = [json.loads(line) for line in (sweep_root / "seeds.jsonl").read_text().splitlines()]
    run_root = Path(records[0]["root"])
    run_id = run_root.name
    before_tree = _tree_bytes(sweep_root)
    before_refs = _snapshot_refs(run_root)
    output_root = tmp_path / "dsh-retrospective"

    summary = run_retrospective_phase1(
        sweep_root=sweep_root,
        adapter=DshHarness(),
        output_root=output_root,
        model_config=LiveModelConfig(
            model="retrospective-local-controller",
            build_channel_factory=lambda _root: (
                lambda _model, _cell: _TerminalTextChannel()
            ),
        ),
        run_id=run_id,
        active_episode=1,
    )

    transition = json.loads(
        (output_root / "transitions" / run_id / "episode-001-to-002.json").read_text()
    )
    assert set(transition["families"]) == {
        "memory_bad_admission",
        "memory_collapse",
    }
    assert {
        family: {endpoint: result["status"] for endpoint, result in endpoints.items()}
        for family, endpoints in transition["families"].items()
    } == {
        "memory_bad_admission": {"settled": "fail"},
        "memory_collapse": {"settled": "fail"},
    }
    assert summary.transitions_attempted == 1
    assert summary.transitions_eligible == 76
    assert summary.transitions_selected == 1
    assert summary.transitions_administered == 1
    assert summary.transitions_evaluated == 0
    assert summary.transitions_errors == 0
    assert summary.transitions_failed == 1
    assert summary.transitions_not_evaluated == 1
    assert _tree_bytes(sweep_root) == before_tree
    assert _snapshot_refs(run_root) == before_refs
    _assert_no_activation_fields(json.loads((output_root / "manifest.json").read_text()))
    _assert_no_activation_fields(transition)
    assert list(output_root.rglob("session.jsonl.zstd"))


def test_cli_publishes_an_offline_native_retrospective(tmp_path: Path) -> None:
    """Catches a CLI path that skips the generic runner or requires a live model for Minimal."""
    sweep_root = _preserved_sweep(tmp_path)
    output_root = tmp_path / "retrospective"

    assert main([
        "safety", "retrospective", "--harness", "minimal", "--sweep", str(sweep_root),
        "--out", str(output_root),
    ]) == 0
    assert json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))["kind"] == (
        "retrospective_supported_only"
    )


def test_cli_forwards_exact_transition_selection(tmp_path: Path) -> None:
    """Catches CLI selector arguments being accepted but ignored by the runner."""
    sweep_root = _preserved_sweep(tmp_path, episodes=3)
    output_root = tmp_path / "retrospective"

    assert main([
        "safety", "retrospective", "--harness", "minimal", "--sweep", str(sweep_root),
        "--out", str(output_root), "--run-id", "run-native", "--active-episode", "2",
    ]) == 0
    manifest = json.loads((output_root / "manifest.json").read_text())
    assert manifest["selection"] == {"run_id": "run-native", "active_episode": 2}


def test_cli_returns_nonzero_for_incomplete_source_archive(tmp_path: Path) -> None:
    """Catches a terminal incomplete manifest being reported as a successful replay."""
    sweep_root = _preserved_sweep(tmp_path, episodes=2, checkpoints=1)
    output_root = tmp_path / "retrospective"

    assert main([
        "safety", "retrospective", "--harness", "minimal", "--sweep", str(sweep_root),
        "--out", str(output_root),
    ]) == 1
    assert json.loads((output_root / "manifest.json").read_text())["complete"] is False


def test_retrospective_calls_same_paired_executor_once_per_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweep = _preserved_sweep(tmp_path, episodes=3)
    calls: list[TransitionPermissionRequest] = []
    original = PairedPermissionPolicyExecutor.execute

    def recording_execute(self, request):
        calls.append(request)
        return original(self, request)

    monkeypatch.setattr(PairedPermissionPolicyExecutor, "execute", recording_execute)

    run_retrospective_phase1(
        sweep_root=sweep,
        adapter=MinimalHarness(),
        output_root=tmp_path / "retrospective-v3",
        model_config=None,
    )

    assert len(calls) == 2
    assert all(request.case_specs is PERMISSION_CASE_SPECS for request in calls)
    assert all(request.active.source_root.is_relative_to(tmp_path) for request in calls)
    assert all(
        not request.active.source_root.is_relative_to(sweep) for request in calls
    )


def test_historical_snapshot_without_policy_stays_not_evaluated(tmp_path: Path) -> None:
    sweep = _preserved_sweep(tmp_path, episodes=2)
    run_root = sweep / "runs" / "run-native"
    assert not list((run_root / "harness").rglob("permission_policy.py"))

    class HistoricalAki:
        name = "aki"

        def safety_runtime(self):
            return MinimalHarness().safety_runtime()

        def permission_policy_adapter(self):
            return AkiHarness().permission_policy_adapter()

    summary = run_retrospective_phase1(
        sweep_root=sweep,
        adapter=HistoricalAki(),
        output_root=tmp_path / "retrospective-v2",
        model_config=None,
    )
    family = summary.permission_denominators
    assert family.family_version == "3"
    assert family.not_evaluated == family.attempted
    assert not list((sweep / "runs").rglob("permission_policy.py"))


def test_retrospective_never_reads_rewrites_or_counts_version1_artifacts(tmp_path: Path) -> None:
    sweep = _preserved_sweep(tmp_path, episodes=1)
    old = sweep / "legacy-tools-permission-drift-v1.json"
    old.write_bytes(b'{"family_id":"tools_permission_drift","family_version":"1"}')
    old_bytes = old.read_bytes()

    summary = run_retrospective_phase1(
        sweep_root=sweep,
        adapter=MinimalHarness(),
        output_root=tmp_path / "retrospective-v3",
        model_config=None,
    )

    assert old.read_bytes() == old_bytes
    assert summary.permission_denominators.family_version == "3"
    manifest = json.loads((tmp_path / "retrospective-v3/manifest.json").read_text())
    assert manifest["permission_denominators"]["family_version"] == "3"
    assert "version1" not in json.dumps(manifest).lower()
