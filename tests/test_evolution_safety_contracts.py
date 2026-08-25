from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from proteus.adapters.minimal import MinimalHarness
from proteus.adapters.minimal_safety import MinimalSafetyRuntime
from proteus.adapters.llm import LLMHarness
from proteus.core.activation import CandidateGateContext
from proteus.core.adapter import ActionEvent
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import EvidenceCellObservation
from proteus.safety.gate import _load_lineage, build_candidate_gate_factory
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


def test_llm_safety_selection_rejects_inherited_minimal_runtime_preflight(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="LLM safety runtime is not implemented"):
        build_candidate_gate_factory(
            adapter_factory=lambda: LLMHarness(key="fixture"),
            suite_spec="proteus.safety.phase1:SUITE",
            safety_model="gpt-5.6-luna",
            controller_root=tmp_path / "must-not-exist",
        )

    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize(
    ("family_id", "reason"),
    (
        ("memory_bad_admission", "no_exact_controlled_state_event"),
        ("tools_permission_drift", "no_exact_proposal"),
    ),
)
def test_terminal_generic_minimal_trace_is_not_behavior_evidence(
    tmp_path: Path, family_id: str, reason: str
) -> None:
    definition = next(item for item in SUITE.definitions() if item.family_id == family_id)
    observation = PHASE1_EXECUTORS[family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MinimalHarness().safety_runtime(),
            context=_context(tmp_path, f"generic-trace-{family_id}"),
            channel=None,
        )
    )
    real_episode = next(
        cell for cell in observation.cells
        if cell.stratum is EvidenceStratum.REAL_EPISODE
    )

    assert real_episode.status is SafetyStatus.NOT_EVALUATED
    assert real_episode.reason == reason
    assert observation.statuses.behavior is SafetyStatus.NOT_EVALUATED
    if family_id == "memory_bad_admission":
        assert observation.unsafe_states
        assert all(
            state.influenced_episode.name == "UNKNOWN"
            for state in observation.unsafe_states
        )


def test_pending_transition_is_not_completed_archive_lineage(tmp_path: Path) -> None:
    definition = SUITE.definitions()[0]
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MinimalHarness().safety_runtime(),
            context=_context(tmp_path, "pending-lineage"),
            channel=None,
        )
    )
    archive = next(
        cell for cell in observation.cells
        if cell.stratum is EvidenceStratum.ARCHIVE_LINEAGE
    )

    assert archive.status is SafetyStatus.NOT_EVALUATED
    assert archive.reason == "no_completed_archive_transition"
    assert not archive.evidence_refs
    assert observation.archive_lineage
    assert all(not item.available and not item.records
               for item in observation.archive_lineage)


def test_episode_one_gate_context_has_no_completed_archive_history(tmp_path: Path) -> None:
    context = CandidateGateContext(
        run_id="run-1",
        episode=1,
        active=SnapshotRef("run-1", 0, SnapshotRole.ACTIVE),
        candidate=SnapshotRef("run-1", 1, SnapshotRole.CANDIDATE),
        active_root=tmp_path / "active",
        candidate_root=tmp_path / "candidate",
        events=(),
    )

    assert _load_lineage(tmp_path, context) == ()


def test_delivered_but_incomplete_fault_cannot_enter_restoration_evaluation(
    tmp_path: Path,
) -> None:
    class IncompleteFaultRuntime(MinimalSafetyRuntime):
        def inject_memory_fault(self, request, context):
            return replace(super().inject_memory_fault(request, context), completed=False)

    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_collapse"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=IncompleteFaultRuntime(MinimalHarness()),
            context=_context(tmp_path, "incomplete-fault"),
            channel=None,
        )
    )
    by_stratum: dict[EvidenceStratum, EvidenceCellObservation] = {
        cell.stratum: cell for cell in observation.cells
    }

    assert by_stratum[EvidenceStratum.NATIVE_BOUNDARY].status is SafetyStatus.NOT_EVALUATED
    assert by_stratum[EvidenceStratum.NATIVE_BOUNDARY].reason == (
        "controlled_fault_not_completed"
    )
    assert by_stratum[EvidenceStratum.REAL_EPISODE].status is SafetyStatus.NOT_EVALUATED
    assert observation.statuses.recovery is SafetyStatus.NOT_EVALUATED


def test_exact_effect_events_use_the_post_episode_oracle(tmp_path: Path) -> None:
    class PostEpisodeCommitRuntime(MinimalSafetyRuntime):
        def run_safety_episode(self, prompts, context, channel):
            result = super().run_safety_episode(prompts, context, channel)
            protected = context.snapshot_root / "tools" / "protected" / "marker.txt"
            protected.parent.mkdir(parents=True, exist_ok=True)
            protected.write_text("protected-effect-must-not-commit\n", encoding="utf-8")
            controlled = (
                ActionEvent(
                    turn=100,
                    phase="act",
                    tool="write_file",
                    surface="tools",
                    params={"effect_id": "phase1-protected-effect"},
                ),
                ActionEvent(
                    turn=101,
                    phase="act",
                    tool="write_file",
                    surface="tools",
                    params={"effect_id": "phase1-allowed-effect"},
                ),
            )
            return replace(result, events=result.events + controlled)

    definition = next(
        item for item in SUITE.definitions() if item.family_id == "tools_permission_drift"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=PostEpisodeCommitRuntime(MinimalHarness()),
            context=_context(tmp_path, "post-episode-effect"),
            channel=None,
        )
    )
    real_episode = next(
        cell for cell in observation.cells
        if cell.stratum is EvidenceStratum.REAL_EPISODE
    )

    assert real_episode.status is SafetyStatus.FAIL
    assert real_episode.violation is True
    assert observation.statuses.behavior is SafetyStatus.FAIL


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
