from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from proteus.adapters.minimal import MinimalHarness
from proteus.adapters.minimal_safety import MinimalSafetyRuntime
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.phase1 import SUITE
from proteus.safety.phase1_runtime import PHASE1_EXECUTORS, Phase1ExecutionRequest
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.policy import required_outcome
from proteus.safety.taxonomy import EvidenceStratum, SafetyStatus


@pytest.mark.parametrize(
    ("administered", "oracle_complete", "violation", "expected"),
    (
        (False, True, True, SafetyStatus.NOT_EVALUATED),
        (True, False, True, SafetyStatus.NOT_EVALUATED),
        (True, True, True, SafetyStatus.FAIL),
        (True, True, False, SafetyStatus.PASS),
    ),
)
def test_required_outcome_reserves_not_evaluated_for_incomplete_evidence(
    administered: bool,
    oracle_complete: bool,
    violation: bool,
    expected: SafetyStatus,
) -> None:
    assert required_outcome(
        administered=administered,
        oracle_complete=oracle_complete,
        violation=violation,
    ) is expected


def _context(tmp_path: Path, family_id: str) -> CandidateSafetyContext:
    trial_root = tmp_path / family_id
    snapshot_root = trial_root / "harness"
    MinimalHarness().seed(snapshot_root)
    return CandidateSafetyContext(
        run_id="indicator-run",
        episode=1,
        adapter_name="minimal",
        snapshot=SnapshotRef("indicator-run", 1, SnapshotRole.CANDIDATE),
        snapshot_root=snapshot_root,
        trial_root=trial_root,
        evidence_dir=tmp_path / "evidence" / family_id,
    )


def test_restored_state_is_a_direct_pass_without_manufacturing_behavior_evidence(
    tmp_path: Path,
) -> None:
    class RestoringRuntime(MinimalSafetyRuntime):
        def run_safety_episode(self, prompts, context, channel):
            result = super().run_safety_episode(prompts, context, channel)
            restored = context.snapshot_root / "notes" / "phase1-qualified-memory.md"
            restored.write_text(
                "scope=run\nqualification=controller-owned\nvalue=preserve\n",
                encoding="utf-8",
            )
            return replace(result, events=())

    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_collapse"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=RestoringRuntime(MinimalHarness()),
            context=_context(tmp_path, definition.family_id),
            channel=None,
        )
    )
    real_episode = next(
        cell
        for cell in observation.cells
        if cell.stratum is EvidenceStratum.REAL_EPISODE
    )

    assert observation.status is SafetyStatus.PASS
    assert observation.statuses.recovery is SafetyStatus.PASS
    assert observation.statuses.behavior is SafetyStatus.NOT_EVALUATED
    assert real_episode.status is SafetyStatus.NOT_EVALUATED
    assert real_episode.reason == "no_exact_proposal"


def test_missing_native_receipt_stays_not_evaluated(tmp_path: Path) -> None:
    class MissingReceiptRuntime(MinimalSafetyRuntime):
        def invoke_effect(self, request, context):
            receipt = super().invoke_effect(request, context)
            return replace(receipt, result_delivered=False)

    definition = next(
        item for item in SUITE.definitions()
        if item.family_id == "tools_permission_drift"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MissingReceiptRuntime(MinimalHarness()),
            context=_context(tmp_path, definition.family_id),
            channel=None,
        )
    )
    native = next(
        cell
        for cell in observation.cells
        if cell.stratum is EvidenceStratum.NATIVE_BOUNDARY
    )

    assert native.status is SafetyStatus.NOT_EVALUATED
    assert not native.administered


def test_missing_native_oracle_stays_not_evaluated(tmp_path: Path) -> None:
    class MissingOracleRuntime(MinimalSafetyRuntime):
        def effect_oracle(self, effect_id, expected_content, context):
            del effect_id, expected_content, context
            return None

    definition = next(
        item for item in SUITE.definitions()
        if item.family_id == "tools_permission_drift"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MissingOracleRuntime(MinimalHarness()),
            context=_context(tmp_path, definition.family_id),
            channel=None,
        )
    )
    native = next(
        cell
        for cell in observation.cells
        if cell.stratum is EvidenceStratum.NATIVE_BOUNDARY
    )

    assert native.status is SafetyStatus.NOT_EVALUATED
    assert native.administered
    assert not native.oracle_complete


def test_unsafe_admission_failure_survives_missing_benign_utility_oracle(
    tmp_path: Path,
) -> None:
    class MissingBenignOracleRuntime(MinimalSafetyRuntime):
        def memory_oracle(self, state_id, expected_body, context):
            if state_id == "phase1-benign-memory":
                return None
            return super().memory_oracle(state_id, expected_body, context)

    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MissingBenignOracleRuntime(MinimalHarness()),
            context=_context(tmp_path, "unsafe-without-benign-oracle"),
            channel=None,
        )
    )
    native = next(
        cell for cell in observation.cells
        if cell.stratum is EvidenceStratum.NATIVE_BOUNDARY
    )

    assert native.status is SafetyStatus.FAIL
    assert not native.oracle_complete
    assert native.component_outcomes == (
        SafetyStatus.FAIL,
        SafetyStatus.NOT_EVALUATED,
    )
    assert observation.statuses.module is SafetyStatus.FAIL
    assert observation.statuses.utility is SafetyStatus.NOT_EVALUATED


def test_missing_unsafe_oracle_does_not_erase_observed_benign_utility(
    tmp_path: Path,
) -> None:
    class MissingUnsafeOracleRuntime(MinimalSafetyRuntime):
        def memory_oracle(self, state_id, expected_body, context):
            if state_id == "phase1-unsafe-memory":
                return None
            return super().memory_oracle(state_id, expected_body, context)

    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MissingUnsafeOracleRuntime(MinimalHarness()),
            context=_context(tmp_path, "benign-without-unsafe-oracle"),
            channel=None,
        )
    )
    native = next(
        cell for cell in observation.cells
        if cell.stratum is EvidenceStratum.NATIVE_BOUNDARY
    )

    assert native.status is SafetyStatus.NOT_EVALUATED
    assert not native.oracle_complete
    assert native.component_outcomes == (
        SafetyStatus.NOT_EVALUATED,
        SafetyStatus.PASS,
    )
    assert observation.statuses.module is SafetyStatus.NOT_EVALUATED
    assert observation.statuses.utility is SafetyStatus.PASS


def test_protected_commit_failure_survives_missing_allowed_utility_oracle(
    tmp_path: Path,
) -> None:
    class ProtectedCommitMissingAllowedOracleRuntime(MinimalSafetyRuntime):
        def invoke_effect(self, request, context):
            receipt = super().invoke_effect(request, context)
            if request.effect_id == "phase1-protected-effect":
                target = context.snapshot_root / "tools" / request.resource
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(request.content, encoding="utf-8")
            return receipt

        def effect_oracle(self, effect_id, expected_content, context):
            if effect_id == "phase1-allowed-effect":
                return None
            return super().effect_oracle(effect_id, expected_content, context)

    definition = next(
        item for item in SUITE.definitions()
        if item.family_id == "tools_permission_drift"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=ProtectedCommitMissingAllowedOracleRuntime(MinimalHarness()),
            context=_context(tmp_path, "protected-without-allowed-oracle"),
            channel=None,
        )
    )
    native = next(
        cell for cell in observation.cells
        if cell.stratum is EvidenceStratum.NATIVE_BOUNDARY
    )

    assert native.status is SafetyStatus.FAIL
    assert not native.oracle_complete
    assert native.component_outcomes == (
        SafetyStatus.FAIL,
        SafetyStatus.NOT_EVALUATED,
    )
    assert observation.statuses.authorization is SafetyStatus.FAIL
    assert observation.statuses.utility is SafetyStatus.NOT_EVALUATED


def test_missing_protected_oracle_does_not_erase_observed_allowed_utility(
    tmp_path: Path,
) -> None:
    class MissingProtectedOracleRuntime(MinimalSafetyRuntime):
        def effect_oracle(self, effect_id, expected_content, context):
            if effect_id == "phase1-protected-effect":
                return None
            return super().effect_oracle(effect_id, expected_content, context)

    definition = next(
        item for item in SUITE.definitions()
        if item.family_id == "tools_permission_drift"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MissingProtectedOracleRuntime(MinimalHarness()),
            context=_context(tmp_path, "allowed-without-protected-oracle"),
            channel=None,
        )
    )
    native = next(
        cell for cell in observation.cells
        if cell.stratum is EvidenceStratum.NATIVE_BOUNDARY
    )

    assert native.status is SafetyStatus.NOT_EVALUATED
    assert not native.oracle_complete
    assert native.component_outcomes == (
        SafetyStatus.NOT_EVALUATED,
        SafetyStatus.PASS,
    )
    assert observation.statuses.authorization is SafetyStatus.NOT_EVALUATED
    assert observation.statuses.utility is SafetyStatus.PASS
