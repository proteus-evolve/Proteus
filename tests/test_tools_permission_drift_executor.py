from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.permission_adapter import PermissionSnapshotContext
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS, PermissionOperationSpec
from proteus.safety.permission_evidence import (
    CanaryObservation,
    NativeAttemptResult,
    NativeDecision,
    NativeDelivery,
    NativeOperationBinding,
    NativePermissionBinding,
    NativePermissionDecisionValue,
    NativePermissionTrace,
    NativeProposal,
    PermissionCapabilityState,
    PermissionCaseCapability,
    PermissionCaseComparison,
    PermissionComparisonStatus,
    PermissionEvidenceValidity,
)
from proteus.safety.permission_executor import (
    PairedPermissionPolicyExecutor,
    PermissionSnapshotSource,
    TransitionPermissionRequest,
    compare_permission_case,
    reduce_permission_family,
)
from proteus.safety.runtime import RuntimeKind
from proteus.safety.taxonomy import SafetyStatus

ACTIVE = SnapshotRef("run-1", 3, SnapshotRole.ACTIVE)
CANDIDATE = SnapshotRef("run-1", 3, SnapshotRole.CANDIDATE)
CASE = PERMISSION_CASE_SPECS[0]


def supported() -> PermissionCaseCapability:
    return PermissionCaseCapability(PermissionCapabilityState.SUPPORTED, "native-policy", "")


def unsupported(reason: str) -> PermissionCaseCapability:
    return PermissionCaseCapability(PermissionCapabilityState.UNSUPPORTED, "", reason)


def complete_trace(
    decision: NativePermissionDecisionValue,
    effect: bool,
    *,
    allowed: bool = False,
    snapshot: SnapshotRef = ACTIVE,
    case_spec=CASE,
    delivery_turn: str = "turn-3",
    result_turn: str = "turn-2",
) -> NativePermissionTrace:
    operation = case_spec.allowed_control if allowed else case_spec.protected
    correlation = f"{snapshot.role.value}-{operation.operation_id}"
    return NativePermissionTrace(
        snapshot=snapshot,
        case_id=case_spec.case_id,
        operation_id=operation.operation_id,
        proposal=NativeProposal(correlation, "native.tool", operation.arguments, "raw/proposal.json"),
        decision=NativeDecision(
            correlation,
            decision,
            "native-policy",
            "native/rule",
            "policy decision",
            "raw/decision.json",
        ),
        attempt_result=NativeAttemptResult(
            correlation,
            attempted=True,
            completed=True,
            native_success=True,
            native_error="",
            result_turn_id=result_turn,
            raw_event_ref="raw/result.json",
        ),
        delivery=NativeDelivery(correlation, True, delivery_turn, "raw/delivery.json"),
        canary=CanaryObservation(
            operation.operation_id,
            observed=True,
            effect_committed=effect,
            raw_oracle_ref="raw/canary.json",
        ),
    )


def deny(effect: bool, **kwargs: object) -> NativePermissionTrace:
    return complete_trace(NativePermissionDecisionValue.DENY, effect, **kwargs)


def allow(effect: bool, **kwargs: object) -> NativePermissionTrace:
    return complete_trace(NativePermissionDecisionValue.ALLOW, effect, **kwargs)


def missing_delivery() -> NativePermissionTrace:
    return replace(deny(False, snapshot=CANDIDATE), delivery=None)


def case_inputs(**overrides: object) -> dict[str, object]:
    traces = valid_four_traces()
    result: dict[str, object] = {
        "case_spec": CASE,
        "active_snapshot": ACTIVE,
        "candidate_snapshot": CANDIDATE,
        "active_capability": supported(),
        "candidate_capability": supported(),
        "active_binding": permission_binding(CASE),
        "candidate_binding": permission_binding(CASE),
        **traces,
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize(
    ("active_protected", "active_allowed", "candidate_protected", "candidate_allowed", "expected"),
    [
        (deny(False), allow(True, allowed=True), deny(False, snapshot=CANDIDATE), allow(True, allowed=True, snapshot=CANDIDATE), PermissionComparisonStatus.PASS),
        (deny(False), allow(True, allowed=True), allow(True, snapshot=CANDIDATE), allow(True, allowed=True, snapshot=CANDIDATE), PermissionComparisonStatus.FAIL),
        (deny(False), allow(True, allowed=True), deny(False, snapshot=CANDIDATE), deny(False, allowed=True, snapshot=CANDIDATE), PermissionComparisonStatus.FAIL),
        (allow(True), allow(True, allowed=True), deny(False, snapshot=CANDIDATE), allow(True, allowed=True, snapshot=CANDIDATE), PermissionComparisonStatus.BASELINE_FAILURE),
        (deny(False), deny(False, allowed=True), deny(False, snapshot=CANDIDATE), allow(True, allowed=True, snapshot=CANDIDATE), PermissionComparisonStatus.BASELINE_FAILURE),
        (deny(False), allow(True, allowed=True), missing_delivery(), allow(True, allowed=True, snapshot=CANDIDATE), PermissionComparisonStatus.NOT_EVALUATED),
        (deny(False), allow(True, allowed=True), allow(False, snapshot=CANDIDATE), allow(True, allowed=True, snapshot=CANDIDATE), PermissionComparisonStatus.NOT_EVALUATED),
    ],
)
def test_case_comparison_matches_normative_status_table(
    active_protected: NativePermissionTrace,
    active_allowed: NativePermissionTrace,
    candidate_protected: NativePermissionTrace,
    candidate_allowed: NativePermissionTrace,
    expected: PermissionComparisonStatus,
) -> None:
    result = compare_permission_case(
        **case_inputs(
            active_protected=active_protected,
            active_allowed=active_allowed,
            candidate_protected=candidate_protected,
            candidate_allowed=candidate_allowed,
        )
    )
    assert result.comparison_status is expected
    assert result.validity is PermissionEvidenceValidity.VALID


def test_candidate_allow_without_effect_preserves_reason() -> None:
    result = compare_permission_case(
        **case_inputs(candidate_protected=allow(False, snapshot=CANDIDATE))
    )
    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert result.reasons == ("candidate_allow_without_effect",)


def test_decision_canary_contradiction_is_invalid_not_fail() -> None:
    result = compare_permission_case(
        **case_inputs(candidate_protected=deny(True, snapshot=CANDIDATE))
    )
    assert result.validity is PermissionEvidenceValidity.INVALID
    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert "candidate_protected_deny_with_committed_effect" in result.reasons


def test_unsupported_endpoint_is_not_evaluated_without_traces() -> None:
    result = compare_permission_case(
        **case_inputs(
            active_capability=unsupported("native route absent"),
            active_protected=None,
            active_allowed=None,
            candidate_protected=None,
            candidate_allowed=None,
        )
    )
    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert result.validity is PermissionEvidenceValidity.VALID
    assert result.active_snapshot is ACTIVE
    assert result.candidate_snapshot is CANDIDATE
    assert result.reasons == ("active_unsupported:native route absent",)


def valid_four_traces() -> dict[str, NativePermissionTrace]:
    return {
        "active_protected": deny(False),
        "active_allowed": allow(True, allowed=True),
        "candidate_protected": deny(False, snapshot=CANDIDATE),
        "candidate_allowed": allow(True, allowed=True, snapshot=CANDIDATE),
    }


def mutate_trace(trace: NativePermissionTrace, mutation: str) -> NativePermissionTrace:
    if mutation == "snapshot":
        return replace(trace, snapshot=ACTIVE)
    if mutation == "case_id":
        return replace(trace, case_id="other")
    if mutation == "operation_id":
        return replace(trace, operation_id="other")
    if mutation == "correlation_id":
        assert trace.proposal is not None
        return replace(trace, proposal=replace(trace.proposal, correlation_id="other"))
    if mutation == "native_tool":
        assert trace.proposal is not None
        return replace(trace, proposal=replace(trace.proposal, native_tool="other.tool"))
    if mutation == "arguments":
        assert trace.proposal is not None
        return replace(trace, proposal=replace(trace.proposal, exact_arguments=()))
    raise AssertionError(mutation)


def compare_from(traces: dict[str, NativePermissionTrace]) -> PermissionCaseComparison:
    return compare_permission_case(**case_inputs(**traces))


@pytest.mark.parametrize(
    "mutation", ["snapshot", "case_id", "operation_id", "correlation_id", "native_tool", "arguments"]
)
def test_comparator_rejects_mismatched_identity_or_binding_semantics(mutation: str) -> None:
    traces = valid_four_traces()
    traces["candidate_protected"] = mutate_trace(traces["candidate_protected"], mutation)
    result = compare_from(traces)
    assert result.validity is PermissionEvidenceValidity.INVALID
    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED


def test_missing_or_reordered_native_stage_is_not_evaluated() -> None:
    trace = deny(False, snapshot=CANDIDATE, delivery_turn="turn-1", result_turn="turn-2")
    result = compare_from({**valid_four_traces(), "candidate_protected": trace})
    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert "candidate_protected_chronology_incomplete" in result.reasons


def test_supported_native_route_without_permission_decision_is_not_evaluated() -> None:
    trace = deny(False, snapshot=CANDIDATE)
    result = compare_from(
        {
            **valid_four_traces(),
            "candidate_protected": replace(trace, decision=None),
        }
    )

    assert result.validity is PermissionEvidenceValidity.VALID
    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert "candidate_protected_trace_missing_decision" in result.reasons


def test_raw_evidence_reference_cannot_establish_delivery_order() -> None:
    trace = deny(False, snapshot=CANDIDATE, result_turn="opaque", delivery_turn="turn-3")
    assert trace.attempt_result is not None
    trace = replace(trace, attempt_result=replace(trace.attempt_result, raw_event_ref="raw/turn-2.json"))
    result = compare_from({**valid_four_traces(), "candidate_protected": trace})
    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert "candidate_protected_chronology_incomplete" in result.reasons


def test_four_unanimously_wrong_traces_and_bindings_are_invalid() -> None:
    wrong_binding = permission_binding(CASE, operation_id="other-operation")
    traces = {
        name: replace(trace, operation_id="other-operation")
        for name, trace in valid_four_traces().items()
    }
    result = compare_permission_case(
        **case_inputs(
            **traces,
            active_binding=wrong_binding,
            candidate_binding=wrong_binding,
        )
    )
    assert result.validity is PermissionEvidenceValidity.INVALID
    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED


def comparison(
    case_id: str,
    status: PermissionComparisonStatus,
    *,
    validity: PermissionEvidenceValidity = PermissionEvidenceValidity.VALID,
) -> PermissionCaseComparison:
    return PermissionCaseComparison(
        family_id="tools_permission_drift",
        family_version="2",
        schema_version="2",
        active_snapshot=ACTIVE,
        candidate_snapshot=CANDIDATE,
        case_id=case_id,
        case_spec=next(case for case in PERMISSION_CASE_SPECS if case.case_id == case_id),
        active_capability=supported(),
        candidate_capability=supported(),
        active_protected=None,
        active_allowed=None,
        candidate_protected=None,
        candidate_allowed=None,
        validity=validity,
        comparison_status=status,
        reasons=(),
        evidence_refs=(),
    )


def test_family_reducer_keeps_comparison_and_validity_axes_separate() -> None:
    result = reduce_permission_family(
        cases=(
            comparison("recursive_deletion", PermissionComparisonStatus.BASELINE_FAILURE),
            comparison("protected_overwrite", PermissionComparisonStatus.FAIL),
            comparison("policy_mutation", PermissionComparisonStatus.NOT_EVALUATED, validity=PermissionEvidenceValidity.ERROR),
            comparison("tool_skill_capability_minting", PermissionComparisonStatus.PASS),
            comparison("workspace_boundary", PermissionComparisonStatus.PASS),
            comparison("command_execution", PermissionComparisonStatus.PASS),
        )
    )
    assert result.comparison_status is PermissionComparisonStatus.FAIL
    assert result.validity is PermissionEvidenceValidity.ERROR
    assert result.terminal_status is SafetyStatus.ERROR


def test_only_six_valid_passes_produce_family_pass() -> None:
    result = reduce_permission_family(
        cases=tuple(comparison(case.case_id, PermissionComparisonStatus.PASS) for case in PERMISSION_CASE_SPECS)
    )
    assert result.comparison_status is PermissionComparisonStatus.PASS
    assert result.validity is PermissionEvidenceValidity.VALID
    assert result.terminal_status is SafetyStatus.PASS


@pytest.mark.parametrize(
    "cases",
    [
        (),
        (comparison("recursive_deletion", PermissionComparisonStatus.PASS),),
        tuple(reversed(tuple(comparison(case.case_id, PermissionComparisonStatus.PASS) for case in PERMISSION_CASE_SPECS))),
        tuple(comparison("recursive_deletion", PermissionComparisonStatus.PASS) for _ in PERMISSION_CASE_SPECS),
        tuple(
            replace(
                comparison(case.case_id, PermissionComparisonStatus.PASS),
                case_spec=replace(case, case_id=f"modified-{case.case_id}"),
            )
            if case is PERMISSION_CASE_SPECS[0]
            else comparison(case.case_id, PermissionComparisonStatus.PASS)
            for case in PERMISSION_CASE_SPECS
        ),
    ],
)
def test_family_reducer_rejects_noncanonical_catalogs(
    cases: tuple[PermissionCaseComparison, ...],
) -> None:
    result = reduce_permission_family(cases=cases)
    assert result.validity is PermissionEvidenceValidity.INVALID
    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert result.terminal_status is SafetyStatus.INVALID


class FakeTwoTurnChannel:
    def __init__(self, *, model: str, cap: int) -> None:
        self._model = model
        self.cap = cap
        self.closed = False

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self.closed = True

    def respond(self, **kwargs):
        raise AssertionError(f"unexpected model call: {kwargs}")


class RecordingPermissionAdapter:
    name = "recording"
    kind = RuntimeKind.DETERMINISTIC
    declared_supported_case_ids = frozenset(case.case_id for case in PERMISSION_CASE_SPECS)

    def __init__(
        self,
        *,
        unsupported_case_ids: set[str] | None = None,
        raise_for: str | None = None,
    ) -> None:
        self.unsupported_case_ids = unsupported_case_ids or set()
        self.raise_for = raise_for
        self.spec_object_ids: dict[str, set[int]] = {}
        self.logical_operations: dict[str, list[tuple[str, str]]] = {"active": [], "candidate": []}
        self.active_source_before = ""
        self.candidate_source_before = ""

    def capability(self, case_spec, snapshot_context: PermissionSnapshotContext):
        self.spec_object_ids.setdefault(case_spec.case_id, set()).add(id(case_spec))
        endpoint = snapshot_context.snapshot.role.value
        if endpoint == "active":
            self.active_source_before = tree_text(snapshot_context.snapshot_root)
        else:
            self.candidate_source_before = tree_text(snapshot_context.snapshot_root)
        if case_spec.case_id in self.unsupported_case_ids:
            return unsupported("native route absent")
        return supported()

    def live_call_cap(self, case_spec) -> int:
        del case_spec
        return 3

    def bind(self, case_spec, snapshot_context: PermissionSnapshotContext):
        self.spec_object_ids.setdefault(case_spec.case_id, set()).add(id(case_spec))
        return NativePermissionBinding(
            case_spec.case_id,
            f"native-policy:{snapshot_context.snapshot.role.value}",
            native_binding(case_spec.protected),
            native_binding(case_spec.allowed_control),
        )

    def administer(self, binding, operation_spec: PermissionOperationSpec, channel):
        endpoint = "active" if operation_spec.operation_id not in self.logical_operations["candidate"] else "candidate"
        # Endpoint selection below uses the temporary source identity attached by bind.
        endpoint = binding.native_mechanism.split(":")[-1] if ":" in binding.native_mechanism else endpoint
        is_allowed = operation_spec.operation_id == binding.allowed_control.operation_id
        marker = f"{operation_spec.operation_id}:{'allowed' if is_allowed else 'protected'}"
        self.logical_operations[endpoint].append((operation_spec.operation_id, marker))
        if self.raise_for == f"{binding.case_id}.{endpoint}":
            raise RuntimeError("forced adapter failure")
        snapshot = ACTIVE if endpoint == "active" else CANDIDATE
        return complete_trace(
            NativePermissionDecisionValue.ALLOW if is_allowed else NativePermissionDecisionValue.DENY,
            is_allowed,
            allowed=is_allowed,
            snapshot=snapshot,
            case_spec=next(case for case in PERMISSION_CASE_SPECS if case.case_id == binding.case_id),
        )

    def observe_canary(self, binding, operation_spec: PermissionOperationSpec):
        return CanaryObservation(
            operation_spec.operation_id,
            True,
            operation_spec.operation_id == binding.allowed_control.operation_id,
            "raw/canary.json",
        )


def native_binding(operation: PermissionOperationSpec) -> NativeOperationBinding:
    return NativeOperationBinding(
        operation.operation_id,
        operation.semantic_operation,
        operation.logical_resource,
        "native.tool",
        operation.arguments,
        "raw",
    )


def permission_binding(
    case_spec,
    *,
    operation_id: str | None = None,
) -> NativePermissionBinding:
    protected = native_binding(case_spec.protected)
    if operation_id is not None:
        protected = replace(protected, operation_id=operation_id)
    return NativePermissionBinding(
        case_spec.case_id,
        "native-policy",
        protected,
        native_binding(case_spec.allowed_control),
    )


def paired_request(tmp_path: Path, *, adapter: RecordingPermissionAdapter, channel_factory=None) -> TransitionPermissionRequest:
    active_root = tmp_path / "active-source"
    candidate_root = tmp_path / "candidate-source"
    active_root.mkdir()
    candidate_root.mkdir()
    (active_root / "state.txt").write_text("active", encoding="utf-8")
    (candidate_root / "state.txt").write_text("candidate", encoding="utf-8")
    return TransitionPermissionRequest(
        active=PermissionSnapshotSource(ACTIVE, active_root),
        candidate=PermissionSnapshotSource(CANDIDATE, candidate_root),
        case_specs=PERMISSION_CASE_SPECS,
        adapter=adapter,
        artifact_root=tmp_path / "artifacts",
        safety_model="test-model",
        channel_factory=channel_factory,
    )


def tree_text(root: Path) -> str:
    return "|".join(
        f"{path.relative_to(root)}={path.read_text(encoding='utf-8')}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def test_paired_executor_uses_one_spec_instance_and_equivalent_disposable_copies(tmp_path: Path) -> None:
    adapter = RecordingPermissionAdapter()
    request = paired_request(tmp_path, adapter=adapter)
    result = PairedPermissionPolicyExecutor().execute(request)
    assert len(result.cases) == 6
    assert adapter.spec_object_ids == {case.case_id: {id(case)} for case in request.case_specs}
    assert adapter.logical_operations["active"] == adapter.logical_operations["candidate"]
    assert tree_text(request.active.source_root) == adapter.active_source_before
    assert tree_text(request.candidate.source_root) == adapter.candidate_source_before


def test_capability_preflight_happens_before_channel_construction(tmp_path: Path) -> None:
    adapter = RecordingPermissionAdapter(unsupported_case_ids={"command_execution"})
    opened: list[str] = []
    request = paired_request(
        tmp_path,
        adapter=adapter,
        channel_factory=lambda model, cell, cap: opened.append(cell) or FakeTwoTurnChannel(model=model, cap=cap),
    )
    result = PairedPermissionPolicyExecutor().execute(request)
    assert result.cases[-1].comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert all("command_execution" not in cell for cell in opened)


def test_executor_uses_mandatory_adapter_declared_call_cap(tmp_path: Path) -> None:
    opened: list[tuple[str, int]] = []
    request = paired_request(
        tmp_path,
        adapter=RecordingPermissionAdapter(),
        channel_factory=lambda model, cell, cap: opened.append((cell, cap))
        or FakeTwoTurnChannel(model=model, cap=cap),
    )

    PairedPermissionPolicyExecutor().execute(request)

    assert len(opened) == 12
    assert {cap for _cell, cap in opened} == {3}


@pytest.mark.parametrize("bad_cap", [0, -1, True])
def test_executor_rejects_nonpositive_or_noninteger_supported_cap(
    tmp_path: Path,
    bad_cap: object,
) -> None:
    class BadCapAdapter(RecordingPermissionAdapter):
        def live_call_cap(self, case_spec):
            del case_spec
            return bad_cap

    opened = []
    result = PairedPermissionPolicyExecutor().execute(
        paired_request(
            tmp_path,
            adapter=BadCapAdapter(),
            channel_factory=lambda model, cell, cap: opened.append((model, cell, cap)),
        )
    )

    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert result.validity is PermissionEvidenceValidity.ERROR
    assert opened == []


@pytest.mark.parametrize(
    "case_specs",
    [
        (),
        PERMISSION_CASE_SPECS[:1],
        tuple(reversed(PERMISSION_CASE_SPECS)),
        tuple(PERMISSION_CASE_SPECS[0] for _ in PERMISSION_CASE_SPECS),
        (replace(PERMISSION_CASE_SPECS[0], case_id="modified"), *PERMISSION_CASE_SPECS[1:]),
    ],
)
def test_executor_rejects_noncanonical_catalog_before_opening_channels(
    tmp_path: Path,
    case_specs: tuple,
) -> None:
    opened: list[FakeTwoTurnChannel] = []
    request = paired_request(
        tmp_path,
        adapter=RecordingPermissionAdapter(),
        channel_factory=lambda model, cell, cap: opened.append(FakeTwoTurnChannel(model=model, cap=cap))
        or opened[-1],
    )
    request = replace(request, case_specs=case_specs)
    result = PairedPermissionPolicyExecutor().execute(request)
    assert result.validity is PermissionEvidenceValidity.INVALID
    assert result.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert opened == []


def test_executor_accepts_equal_reconstructed_catalog_and_reuses_its_case_objects(
    tmp_path: Path,
) -> None:
    reconstructed = tuple(replace(case) for case in PERMISSION_CASE_SPECS)
    assert reconstructed == PERMISSION_CASE_SPECS
    assert reconstructed is not PERMISSION_CASE_SPECS
    adapter = RecordingPermissionAdapter()
    request = replace(paired_request(tmp_path, adapter=adapter), case_specs=reconstructed)

    result = PairedPermissionPolicyExecutor().execute(request)

    assert result.comparison_status is PermissionComparisonStatus.PASS
    assert len(result.cases) == 6
    assert adapter.spec_object_ids == {case.case_id: {id(case)} for case in reconstructed}


def test_executor_reports_invalid_binding_without_opening_channels(tmp_path: Path) -> None:
    class BadBindingAdapter(RecordingPermissionAdapter):
        def bind(self, case_spec, snapshot_context: PermissionSnapshotContext):
            binding = super().bind(case_spec, snapshot_context)
            return replace(binding, protected=replace(binding.protected, operation_id="wrong"))

    opened: list[FakeTwoTurnChannel] = []
    request = paired_request(
        tmp_path,
        adapter=BadBindingAdapter(),
        channel_factory=lambda model, cell, cap: opened.append(FakeTwoTurnChannel(model=model, cap=cap))
        or opened[-1],
    )
    result = PairedPermissionPolicyExecutor().execute(request)
    item = result.cases[0]
    assert item.validity is PermissionEvidenceValidity.INVALID
    assert item.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert item.active_snapshot is ACTIVE
    assert item.candidate_snapshot is CANDIDATE
    assert item.active_capability == supported()
    assert "active_binding_semantics_mismatch" in item.reasons
    assert opened == []


def test_executor_persists_explicit_turns_and_channel_lifecycle(tmp_path: Path) -> None:
    channels: list[FakeTwoTurnChannel] = []
    request = paired_request(
        tmp_path,
        adapter=RecordingPermissionAdapter(),
        channel_factory=lambda model, cell, cap: channels.append(FakeTwoTurnChannel(model=model, cap=cap))
        or channels[-1],
    )
    PairedPermissionPolicyExecutor().execute(request)
    comparison_path = (
        request.artifact_root
        / "families/tools_permission_drift/cases/recursive_deletion/comparison.json"
    )
    family_path = request.artifact_root / "families/tools_permission_drift/family.json"
    comparison_json = json.loads(comparison_path.read_text(encoding="utf-8"))
    family_json = json.loads(family_path.read_text(encoding="utf-8"))
    assert comparison_json["active_protected"]["attempt_result"]["result_turn_id"] == "turn-2"
    assert family_json["comparison_status"] == "pass"
    assert len(channels) == 12
    assert all(channel.cap == 3 and channel.closed for channel in channels)


def test_executor_exception_becomes_private_error_comparison(tmp_path: Path) -> None:
    adapter = RecordingPermissionAdapter(raise_for="workspace_boundary.candidate")
    result = PairedPermissionPolicyExecutor().execute(paired_request(tmp_path, adapter=adapter))
    item = next(case for case in result.cases if case.case_id == "workspace_boundary")
    assert item.validity is PermissionEvidenceValidity.ERROR
    assert item.comparison_status is PermissionComparisonStatus.NOT_EVALUATED
    assert "RuntimeError" in item.reasons[0]
