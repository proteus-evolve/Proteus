from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from safety_memory_fixtures import synthetic_advbench

from proteus.adapters.minimal import MinimalHarness
from proteus.adapters.minimal_safety import MinimalSafetyRuntime
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.indicators import EvolutionSafetyIndicators, FamilyIndicatorProjection
from proteus.safety.phase1 import SUITE
from proteus.safety.phase1_runtime import PHASE1_EXECUTORS, Phase1ExecutionRequest
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.policy import evaluate_safety_policy, required_outcome
from proteus.safety.taxonomy import EvidenceStratum, SafetyStatus


def profile_with_terminal_statuses(
    statuses: tuple[SafetyStatus, ...],
) -> EvolutionSafetyIndicators:
    return EvolutionSafetyIndicators(
        tuple(
            FamilyIndicatorProjection(
                family_id=f"family-{index}",
                family_version="1",
                terminal_status=status,
                active_status=None,
                candidate_status=None,
                comparison_status=None,
                evidence_validity=None,
                active_components=None,
                candidate_components=None,
            )
            for index, status in enumerate(statuses)
        )
    )


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((SafetyStatus.PASS,), SafetyStatus.PASS),
        (
            (SafetyStatus.PASS, SafetyStatus.NOT_EVALUATED),
            SafetyStatus.NOT_EVALUATED,
        ),
        ((SafetyStatus.FAIL, SafetyStatus.BASELINE_FAILURE), SafetyStatus.FAIL),
        (
            (SafetyStatus.BASELINE_FAILURE, SafetyStatus.NOT_EVALUATED),
            SafetyStatus.BASELINE_FAILURE,
        ),
        ((SafetyStatus.INVALID, SafetyStatus.FAIL), SafetyStatus.INVALID),
        ((SafetyStatus.ERROR, SafetyStatus.INVALID), SafetyStatus.ERROR),
    ],
)
def test_policy_uses_exact_fail_closed_terminal_precedence(
    statuses: tuple[SafetyStatus, ...], expected: SafetyStatus
) -> None:
    decision = evaluate_safety_policy(profile_with_terminal_statuses(statuses))
    assert decision.status is expected
    assert decision.allowed is (expected is SafetyStatus.PASS)


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


def test_prior_memory_survives_occupancy_probe_without_exact_events(
    tmp_path: Path,
) -> None:
    class SilentRuntime(MinimalSafetyRuntime):
        def run_safety_episode(self, prompts, context, channel):
            result = super().run_safety_episode(prompts, context, channel)
            return replace(result, events=())

    context = _context(tmp_path, "memory_collapse")
    (context.snapshot_root / "notes" / "kept.md").write_text("keep me\n", encoding="utf-8")
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_collapse"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=SilentRuntime(MinimalHarness()),
            context=context,
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
    assert observation.statuses.behavior is SafetyStatus.PASS
    assert real_episode.status is SafetyStatus.PASS
    assert (context.snapshot_root / "notes" / "kept.md").read_text() == "keep me\n"


def test_occupancy_probe_fails_when_prior_memory_is_removed(
    tmp_path: Path,
) -> None:
    class DroppingRuntime(MinimalSafetyRuntime):
        def run_safety_episode(self, prompts, context, channel):
            result = super().run_safety_episode(prompts, context, channel)
            (context.snapshot_root / "notes" / "kept.md").unlink()
            return result

    context = _context(tmp_path, "memory_collapse_drop")
    (context.snapshot_root / "notes" / "kept.md").write_text("keep me\n", encoding="utf-8")
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_collapse"
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=DroppingRuntime(MinimalHarness()),
            context=context,
            channel=None,
        )
    )

    assert observation.status is SafetyStatus.FAIL
    assert observation.statuses.behavior is SafetyStatus.FAIL
    assert observation.statuses.recovery is SafetyStatus.FAIL


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
            advbench_items=synthetic_advbench(),
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
            if state_id.startswith("advbench-"):
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
            advbench_items=synthetic_advbench(),
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
