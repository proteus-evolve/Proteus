from __future__ import annotations

from pathlib import Path

from proteus.adapters.minimal import MinimalHarness
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.phase1 import SUITE
from proteus.safety.phase1_runtime import PHASE1_EXECUTORS, Phase1ExecutionRequest
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import LogicalTransitionRecord, RuntimeKind
from proteus.safety.taxonomy import EvidenceStratum, EvaluationArm, SafetyStatus


def _context(tmp_path: Path, family_id: str) -> CandidateSafetyContext:
    trial_root = tmp_path / family_id
    harness_root = trial_root / "harness"
    MinimalHarness().seed(harness_root)
    candidate = SnapshotRef("run-1", 1, SnapshotRole.CANDIDATE)
    return CandidateSafetyContext(
        run_id="run-1",
        episode=1,
        adapter_name="minimal",
        snapshot=candidate,
        snapshot_root=harness_root,
        trial_root=trial_root,
        evidence_dir=tmp_path / "evidence" / family_id,
        events=(),
        lineage=(
            LogicalTransitionRecord(
                active=SnapshotRef("run-1", 0, SnapshotRole.ACTIVE),
                candidate=candidate,
                activated=None,
                decision_ref="pending",
            ),
        ),
    )


def test_phase1_declares_only_function_oriented_full_harness_cells() -> None:
    expected = {
        EvidenceStratum.NATIVE_BOUNDARY,
        EvidenceStratum.REAL_EPISODE,
        EvidenceStratum.ARCHIVE_LINEAGE,
    }

    for definition in SUITE.definitions():
        assert {cell.stratum for cell in definition.declared_cells} == expected
        assert {cell.arm for cell in definition.declared_cells} == {
            EvaluationArm.FULL_HARNESS
        }


def test_phase1_dispatches_all_family_semantics_from_core(tmp_path: Path) -> None:
    definitions = {item.family_id: item for item in SUITE.definitions()}
    assert set(PHASE1_EXECUTORS) == set(definitions)

    statuses = {}
    for family_id, executor in PHASE1_EXECUTORS.items():
        adapter = MinimalHarness()
        observation = executor(
            Phase1ExecutionRequest(
                definition=definitions[family_id],
                runtime=adapter.safety_runtime(),
                context=_context(tmp_path, family_id),
                channel=None,
            )
        )
        statuses[family_id] = observation.status
        assert observation.family_id == family_id
        assert observation.administered
        assert {cell.stratum for cell in observation.cells} == {
            cell.stratum for cell in definitions[family_id].declared_cells
        }

    assert statuses == {
        "memory_bad_admission": SafetyStatus.FAIL,
        "memory_collapse": SafetyStatus.FAIL,
        "tools_permission_drift": SafetyStatus.PASS,
    }


def test_minimal_runtime_is_deterministic_and_returns_receipts_not_verdicts(
    tmp_path: Path,
) -> None:
    runtime = MinimalHarness().safety_runtime()
    context = _context(tmp_path, "receipt-contract")

    from proteus.safety.runtime import MemoryStateRequest, NativeReceipt

    receipt = runtime.introduce_memory(
        MemoryStateRequest("controlled-state", "controller body", unsafe=True), context
    )

    assert runtime.kind is RuntimeKind.DETERMINISTIC
    assert isinstance(receipt, NativeReceipt)
    assert receipt.completed
    assert not hasattr(receipt, "status")
    assert (context.snapshot_root / "notes" / "controlled-state.md").read_text() == (
        "controller body"
    )
