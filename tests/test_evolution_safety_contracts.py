from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from safety_memory_fixtures import (
    make_paul_graham_panel,
    plant_harness_memory,
    synthetic_advbench,
)

from proteus.adapters.minimal import MinimalHarness
from proteus.adapters.minimal_safety import MinimalSafetyRuntime
from proteus.core.activation import SettledEpisodeSafetyContext
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import EvidenceCellObservation, ProbeEndpoint
from proteus.safety.external_corpus import load_paul_graham_panel
from proteus.safety.gate import _admission_from_observation, _load_lineage
from proteus.safety.indicators import FamilyExecution
from proteus.safety.phase1 import SUITE
from proteus.safety.phase1_runtime import PHASE1_EXECUTORS, Phase1ExecutionRequest
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.policy import evaluate_safety_policy
from proteus.safety.runtime import (
    LogicalTransitionRecord,
    MemoryAccessMode,
    MemoryInventoryRuntime,
    RuntimeKind,
)
from proteus.safety.taxonomy import EvaluationArm, EvidenceStratum, SafetyStatus


def _context(tmp_path: Path, family_id: str) -> CandidateSafetyContext:
    trial_root = tmp_path / family_id
    harness_root = trial_root / "harness"
    MinimalHarness().seed(harness_root)
    plant_harness_memory(harness_root)
    settled = SnapshotRef("run-1", 1, SnapshotRole.ACTIVE)
    candidate = SnapshotRef("run-1", 1, SnapshotRole.CANDIDATE)
    return CandidateSafetyContext(
        run_id="run-1",
        episode=1,
        adapter_name="minimal",
        snapshot=settled,
        snapshot_root=harness_root,
        trial_root=trial_root,
        evidence_dir=tmp_path / "evidence" / family_id,
        endpoint=ProbeEndpoint.SETTLED,
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


def test_every_builtin_runtime_declares_adapter_owned_exact_memory() -> None:
    from proteus.adapters.aki_safety import AkiSafetyRuntime
    from proteus.adapters.dsh import DshHarness
    from proteus.adapters.llm import LLMHarness
    from proteus.adapters.pi import PiHarness

    runtimes = (
        MinimalHarness().safety_runtime(),
        LLMHarness().safety_runtime(),
        PiHarness().safety_runtime(),
        DshHarness().safety_runtime(),
        AkiSafetyRuntime(SimpleNamespace(container=object())),
    )

    assert all(isinstance(runtime, MemoryInventoryRuntime) for runtime in runtimes)
    assert all(
        runtime.memory_access_mode is MemoryAccessMode.EXACT_KEY
        for runtime in runtimes
    )


def test_baseline_failure_is_fail_closed_in_policy() -> None:
    decision = evaluate_safety_policy(
        SimpleNamespace(
            families=(
                SimpleNamespace(
                    family_id="tools_permission_drift",
                    terminal_status=SafetyStatus.BASELINE_FAILURE,
                ),
            )
        )
    )

    assert decision.status is SafetyStatus.BASELINE_FAILURE
    assert not decision.allowed
    assert decision.blockers == ("tools_permission_drift:baseline_failure",)


def test_phase1_dispatches_current_memory_semantics_from_core(tmp_path: Path) -> None:
    definitions = {item.family_id: item for item in SUITE.definitions()}
    assert set(PHASE1_EXECUTORS) == {"memory_bad_admission", "memory_collapse"}
    assert "tools_permission_drift" in definitions

    statuses = {}
    for family_id, executor in PHASE1_EXECUTORS.items():
        adapter = MinimalHarness()
        observation = executor(
            Phase1ExecutionRequest(
                definition=definitions[family_id],
                runtime=adapter.safety_runtime(),
                context=_context(tmp_path, family_id),
                channel=None,
                advbench_items=synthetic_advbench(),
                collapse_panel=(
                    load_paul_graham_panel(
                        make_paul_graham_panel(
                            tmp_path / f"{family_id}-panel", tokens_per_source=1_100
                        )
                    )
                    if family_id == "memory_collapse"
                    else None
                ),
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
        "memory_collapse": SafetyStatus.PASS,
    }
    admission = PHASE1_EXECUTORS["memory_bad_admission"](
        Phase1ExecutionRequest(
            definition=definitions["memory_bad_admission"],
            runtime=MinimalHarness().safety_runtime(),
            context=_context(tmp_path, "admission-levels"),
            channel=None,
            advbench_items=synthetic_advbench(),
        )
    )
    assert admission.statuses.module is SafetyStatus.FAIL
    assert admission.statuses.behavior is SafetyStatus.PASS
    assert admission.statuses.utility is SafetyStatus.PASS
    assert admission.memory_access_mode == MemoryAccessMode.EXACT_KEY.value
    assert admission.memory_seeded_control_ids == ("proteus-memory-control",)
    assert {result.state_id for result in admission.admission_exact_results} == {
        "session",
        "proteus-memory-control",
    }
    assert admission.admission_shadow_results == ()


def test_terminal_generic_minimal_trace_is_non_uptake_behavior_evidence(
    tmp_path: Path,
) -> None:
    family_id = "memory_bad_admission"
    definition = next(item for item in SUITE.definitions() if item.family_id == family_id)
    observation = PHASE1_EXECUTORS[family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MinimalHarness().safety_runtime(),
            context=_context(tmp_path, f"generic-trace-{family_id}"),
            channel=None,
            advbench_items=synthetic_advbench(),
        )
    )
    real_episode = next(
        cell for cell in observation.cells
        if cell.stratum is EvidenceStratum.REAL_EPISODE
    )

    assert observation.statuses.module is SafetyStatus.FAIL
    assert real_episode.status is SafetyStatus.PASS
    assert observation.statuses.behavior is SafetyStatus.PASS
    assert observation.admission_behavior_target == "session"
    assert observation.admission_shadow_results == ()
    behavior_state = next(
        state
        for state in observation.unsafe_states
        if state.state_id == observation.admission_behavior_target
    )
    assert behavior_state.influenced_episode.name == "FALSE"


def test_unscheduled_admission_behavior_is_not_counted_as_a_trial(
    tmp_path: Path,
) -> None:
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MinimalHarness().safety_runtime(),
            context=_context(tmp_path, "unscheduled-admission-behavior"),
            channel=None,
            advbench_items=synthetic_advbench(),
            run_behavior=False,
        )
    )
    indicators = _admission_from_observation(
        observation,
        FamilyExecution(
            schedule_status="evaluated",
            last_observed_episode=1,
            evidence_refs=observation.evidence_refs,
        ),
    )

    assert observation.admission_behavior_target is None
    assert observation.statuses.behavior is SafetyStatus.NOT_EVALUATED
    assert indicators.behavior_trials == 0
    assert indicators.unsafe_followed is None


def test_pending_transition_is_not_completed_archive_lineage(tmp_path: Path) -> None:
    definition = SUITE.definitions()[0]
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MinimalHarness().safety_runtime(),
            context=_context(tmp_path, "pending-lineage"),
            channel=None,
            advbench_items=synthetic_advbench(),
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


def test_required_real_episode_gap_propagates_to_family_and_policy(
    tmp_path: Path,
) -> None:
    class NonterminalRuntime(MinimalSafetyRuntime):
        def run_safety_episode(self, prompts, context, channel):
            return replace(
                super().run_safety_episode(prompts, context, channel),
                terminal=False,
                error="missing required phase",
            )

    definition = next(
        item for item in SUITE.definitions()
        if item.family_id == "memory_collapse"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=NonterminalRuntime(MinimalHarness()),
            context=_context(tmp_path, "required-real-episode"),
            channel=None,
        )
    )
    decision = evaluate_safety_policy(
        SimpleNamespace(
            families=(
                SimpleNamespace(
                    family_id=definition.family_id,
                    terminal_status=observation.status,
                ),
            )
        )
    )

    assert observation.status is SafetyStatus.NOT_EVALUATED
    assert decision.status is SafetyStatus.NOT_EVALUATED
    assert not decision.allowed


def test_episode_one_gate_context_has_no_completed_archive_history(tmp_path: Path) -> None:
    context = SettledEpisodeSafetyContext(
        run_id="run-1",
        episode=1,
        snapshot_ref=SnapshotRef("run-1", 1, SnapshotRole.ACTIVE),
        snapshot_root=tmp_path / "settled",
        trace=(),
    )

    assert _load_lineage(tmp_path, context) == ()


def test_incomplete_anchor_writes_cannot_enter_collapse_evaluation(
    tmp_path: Path,
) -> None:
    class IncompleteAnchorRuntime(MinimalSafetyRuntime):
        def introduce_memory(self, request, context):
            return replace(
                super().introduce_memory(request, context),
                completed=False,
                result_delivered=False,
            )

    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_collapse"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=IncompleteAnchorRuntime(MinimalHarness()),
            context=_context(tmp_path, "incomplete-anchor"),
            channel=None,
            collapse_panel=load_paul_graham_panel(
                make_paul_graham_panel(tmp_path / "panel", tokens_per_source=1_100)
            ),
        )
    )
    by_stratum: dict[EvidenceStratum, EvidenceCellObservation] = {
        cell.stratum: cell for cell in observation.cells
    }

    assert by_stratum[EvidenceStratum.NATIVE_BOUNDARY].status is SafetyStatus.NOT_EVALUATED
    assert by_stratum[EvidenceStratum.NATIVE_BOUNDARY].reason == (
        "collapse_anchor_precondition_failed"
    )
    assert by_stratum[EvidenceStratum.REAL_EPISODE].status is SafetyStatus.NOT_EVALUATED
    assert observation.statuses.recovery is SafetyStatus.NOT_EVALUATED


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


def test_minimal_runtime_inventories_mutable_notes_by_exact_key(tmp_path: Path) -> None:
    from proteus.safety.runtime import MemoryStateRequest

    runtime = MinimalHarness().safety_runtime()
    context = _context(tmp_path, "ordinary-memory-enumeration")
    runtime.introduce_memory(
        MemoryStateRequest("session-shadow", "controller injected body", unsafe=True),
        context,
    )

    records = runtime.enumerate_ordinary_memory(context)

    assert {record.state_id for record in records} == {"session"}
    assert runtime.memory_access_mode is MemoryAccessMode.EXACT_KEY
    assert all(record.source == "notes" and record.trust == "mutable" for record in records)
    assert not hasattr(runtime, "query_memory")


def test_minimal_runtime_has_no_ranked_query_requirement(tmp_path: Path) -> None:
    runtime = MinimalHarness().safety_runtime()
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=runtime,
            context=_context(tmp_path, "exact-key-admission"),
            channel=None,
            advbench_items=synthetic_advbench(),
        )
    )

    assert not hasattr(runtime, "query_memory")
    assert observation.memory_access_mode == MemoryAccessMode.EXACT_KEY.value
    assert observation.admission_shadow_results == ()
    assert observation.statuses.module is SafetyStatus.FAIL
    native = next(
        cell for cell in observation.cells if cell.stratum is EvidenceStratum.NATIVE_BOUNDARY
    )
    assert native.reason == ""
    assert native.component_outcomes == (SafetyStatus.FAIL, SafetyStatus.PASS)


def test_advbench_uptake_fails_behavior_while_keep_fails_module(tmp_path: Path) -> None:
    items = synthetic_advbench()

    class FollowingRuntime(MinimalSafetyRuntime):
        def run_safety_episode(self, prompts, context, channel):
            result = super().run_safety_episode(prompts, context, channel)
            body = (context.snapshot_root / "notes" / "session.md").read_text(
                encoding="utf-8"
            )
            item = next(item for item in items if item.target in body)
            (context.snapshot_root / "notes" / "uptake.md").write_text(
                item.target + "\n", encoding="utf-8"
            )
            return result

    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=FollowingRuntime(MinimalHarness()),
            context=_context(tmp_path, "uptake"),
            channel=None,
            advbench_items=items,
        )
    )

    assert observation.statuses.module is SafetyStatus.FAIL
    assert observation.admission_behavior_target == "session"
    assert observation.statuses.behavior is SafetyStatus.FAIL
    assert observation.status is SafetyStatus.FAIL
