"""Permission evidence must come from the text harnesses' ordinary dispatchers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proteus.adapters.llm import LLMHarness
from proteus.adapters.minimal import MinimalHarness
from proteus.adapters.text_permission import LlmTextPermissionAdapter, MinimalTextPermissionAdapter
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.permission_adapter import PermissionSnapshotContext
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS
from proteus.safety.permission_evidence import (
    NativePermissionDecisionValue,
    PermissionComparisonStatus,
    PermissionEvidenceValidity,
)
from proteus.safety.permission_executor import (
    PairedPermissionPolicyExecutor,
    PermissionSnapshotSource,
    SnapshotPermissionExecutor,
    SnapshotPermissionRequest,
    TransitionPermissionRequest,
)
from proteus.safety.reporting import denominators_from_family


def _case(case_id: str):
    return next(case for case in PERMISSION_CASE_SPECS if case.case_id == case_id)


def _context(tmp_path: Path, harness: MinimalHarness | LLMHarness) -> PermissionSnapshotContext:
    ordinary_root = tmp_path / "ordinary"
    snapshot_root = ordinary_root / "harness"
    harness.seed(snapshot_root)
    artifact_root = tmp_path / "artifacts"
    return PermissionSnapshotContext(
        snapshot=SnapshotRef(harness.name, 1, SnapshotRole.ACTIVE),
        snapshot_root=snapshot_root,
        trial_root=ordinary_root / "trial",
        evidence_dir=artifact_root / "tools_permission_drift" / "raw",
        artifact_root=artifact_root,
    )


def _assert_proposal_decision_attempt(trace) -> None:
    assert trace.proposal is not None
    assert trace.decision is not None
    assert trace.attempt_result is not None


@pytest.mark.parametrize("case_id", ("protected_overwrite", "workspace_boundary"))
def test_minimal_probe_exercises_the_real_write_note_dispatcher(
    tmp_path: Path, case_id: str
) -> None:
    harness = MinimalHarness()
    adapter = MinimalTextPermissionAdapter(harness)
    context = _context(tmp_path, harness)
    case = _case(case_id)

    assert adapter.live_call_cap(case) == 0
    binding = adapter.bind(case, context)
    assert binding is not None
    protected = adapter.administer(binding, case.protected, None)
    allowed = adapter.administer(binding, case.allowed_control, None)

    _assert_proposal_decision_attempt(protected)
    _assert_proposal_decision_attempt(allowed)
    assert protected.delivery is not None
    assert allowed.delivery is not None
    assert protected.delivery.later_turn_id == allowed.delivery.later_turn_id == "turn-3"
    assert protected.decision.value is NativePermissionDecisionValue.ALLOW
    assert allowed.decision.value is NativePermissionDecisionValue.ALLOW
    assert protected.attempt_result.native_success
    assert allowed.attempt_result.native_success
    assert adapter.observe_canary(binding, case.protected).effect_committed
    assert adapter.observe_canary(binding, case.allowed_control).effect_committed
    trace_path = context.evidence_dir / f"{case_id}-ordinary-trace.jsonl"
    assert '"tool": "write_note"' in trace_path.read_text(encoding="utf-8")
    assert "actual_content" in (
        context.evidence_dir
        / f"{case.protected.operation_id.replace('.', '-')}-canary.json"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("case_id", "protected_decision", "protected_effect"),
    (
        (
            "protected_overwrite",
            NativePermissionDecisionValue.ALLOW,
            True,
        ),
        (
            "workspace_boundary",
            NativePermissionDecisionValue.DENY,
            False,
        ),
    ),
)
def test_llm_probe_runs_the_real_json_parser_and_dispatcher(
    tmp_path: Path,
    case_id: str,
    protected_decision: NativePermissionDecisionValue,
    protected_effect: bool,
) -> None:
    harness = LLMHarness()
    adapter = LlmTextPermissionAdapter(harness)
    context = _context(tmp_path, harness)
    case = _case(case_id)
    assert adapter.live_call_cap(case) == 0
    assert adapter.permission_requires_live_channel is False
    binding = adapter.bind(case, context)
    assert binding is not None

    protected = adapter.administer(binding, case.protected, None)
    allowed = adapter.administer(binding, case.allowed_control, None)

    _assert_proposal_decision_attempt(protected)
    _assert_proposal_decision_attempt(allowed)
    assert protected.delivery is not None
    assert allowed.delivery is not None
    assert protected.decision.value is protected_decision
    assert protected.attempt_result.native_success is (protected_decision is NativePermissionDecisionValue.ALLOW)
    assert adapter.observe_canary(binding, case.protected).effect_committed is protected_effect
    assert adapter.observe_canary(binding, case.allowed_control).effect_committed


@pytest.mark.parametrize(
    ("harness", "expected"),
    (
        (
            MinimalHarness(),
            {
                "protected_overwrite": PermissionComparisonStatus.FAIL,
                "workspace_boundary": PermissionComparisonStatus.FAIL,
            },
        ),
        (
            LLMHarness(),
            {
                "protected_overwrite": PermissionComparisonStatus.FAIL,
                "workspace_boundary": PermissionComparisonStatus.PASS,
            },
        ),
    ),
)
def test_paired_text_dispatch_keeps_every_applicable_case_evaluated(
    tmp_path: Path,
    harness: MinimalHarness | LLMHarness,
    expected: dict[str, PermissionComparisonStatus],
) -> None:
    active_root = tmp_path / "active"
    candidate_root = tmp_path / "candidate"
    harness.seed(active_root)
    harness.seed(candidate_root)

    family = PairedPermissionPolicyExecutor().execute(
        TransitionPermissionRequest(
            active=PermissionSnapshotSource(
                SnapshotRef(harness.name, 0, SnapshotRole.ACTIVE), active_root
            ),
            candidate=PermissionSnapshotSource(
                SnapshotRef(harness.name, 1, SnapshotRole.CANDIDATE), candidate_root
            ),
            case_specs=PERMISSION_CASE_SPECS,
            adapter=harness.permission_policy_adapter(),
            artifact_root=tmp_path / "artifacts",
            safety_model="",
            channel_factory=None,
        )
    )

    by_id = {item.case_id: item for item in family.cases}
    denominators = denominators_from_family(family)
    assert family.validity is PermissionEvidenceValidity.VALID
    assert {
        case_id: by_id[case_id].comparison_status for case_id in expected
    } == expected
    assert all(by_id[case_id].validity is PermissionEvidenceValidity.VALID for case_id in expected)
    assert all(
        by_id[case_id].comparison_status is PermissionComparisonStatus.NOT_EVALUATED
        for case_id in set(by_id) - set(expected)
    )
    assert denominators.supported == denominators.administered == 2
    assert denominators.evaluated == 2
    assert denominators.structurally_unsupported == 4
    assert denominators.not_evaluated == 0


@pytest.mark.parametrize(
    ("case_id", "protected_effect"),
    (
        (
            "protected_overwrite",
            True,
        ),
        (
            "workspace_boundary",
            False,
        ),
    ),
)
def test_llm_dispatch_evidence_reaches_a_real_later_phase_input(
    tmp_path: Path,
    case_id: str,
    protected_effect: bool,
) -> None:
    harness = LLMHarness()
    adapter = LlmTextPermissionAdapter(harness)
    context = _context(tmp_path, harness)
    case = _case(case_id)

    family = SnapshotPermissionExecutor().execute(
        SnapshotPermissionRequest(
            source=PermissionSnapshotSource(context.snapshot, context.snapshot_root),
            case_specs=(case,),
            adapter=adapter,
            artifact_root=context.artifact_root,
            safety_model="",
            channel_factory=None,
        )
    )

    evaluation = family.cases[0]
    assert evaluation.validity is PermissionEvidenceValidity.VALID
    assert evaluation.reasons == ()
    assert evaluation.protected_effect_committed is protected_effect
    assert evaluation.allowed_effect_committed is True
    for trace in (evaluation.protected, evaluation.allowed):
        assert trace is not None
        assert trace.delivery is not None
        assert trace.delivery.raw_input_ref.endswith("ordinary-controller-calls.json#call-2")
    calls_path = next(
        context.artifact_root.glob(
            f"tools_permission_drift/raw/*/{case_id}-ordinary-controller-calls.json"
        )
    )
    calls = json.loads(calls_path.read_text(encoding="utf-8"))
    assert len(calls["calls"]) == 4
    assert all(call["origin"] == "controller_local_permission_request" for call in calls["calls"])
    assert "Current harness state:" in calls["calls"][1]["input"]
