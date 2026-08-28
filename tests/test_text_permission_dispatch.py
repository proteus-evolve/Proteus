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
    PermissionEvidenceValidity,
)
from proteus.safety.permission_executor import (
    PermissionSnapshotSource,
    SnapshotPermissionExecutor,
    SnapshotPermissionRequest,
)


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


def test_llm_probe_does_not_depend_on_model_compliance(tmp_path: Path) -> None:
    harness = LLMHarness()
    adapter = LlmTextPermissionAdapter(harness)
    context = _context(tmp_path, harness)
    case = _case("protected_overwrite")
    binding = adapter.bind(case, context)
    assert binding is not None

    trace = adapter.administer(binding, case.protected, None)

    _assert_proposal_decision_attempt(trace)
    assert trace.delivery is not None


def test_minimal_dispatch_records_complete_chain_and_real_protection_failure(
    tmp_path: Path,
) -> None:
    harness = MinimalHarness()
    adapter = MinimalTextPermissionAdapter(harness)
    context = _context(tmp_path, harness)
    case = _case("protected_overwrite")

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
    assert evaluation.protected_effect_committed is True
    assert evaluation.allowed_effect_committed is True
    assert evaluation.reasons == ()


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
