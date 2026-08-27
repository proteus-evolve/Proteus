"""Declared-cell scheduling for controller-owned activation safety."""

from __future__ import annotations

import importlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Callable

from proteus.core.activation import CandidateGateContext, CandidateGateResult
from proteus.core.episode import eval_history_path
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import EvidenceCellObservation, ProbeEndpoint, ProbeObservation
from proteus.safety.indicators import (
    EvolutionSafetyIndicators,
    MatchedFamilyObservations,
    derive_indicator_profile,
)
from proteus.safety.live import LiveModelChannel
from proteus.safety.permission_adapter import PermissionPolicyAdapter, PermissionSnapshotContext
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS
from proteus.safety.permission_evidence import (
    CanaryObservation,
    NativeAttemptResult,
    NativeDecision,
    NativeDelivery,
    NativePermissionDecisionValue,
    NativePermissionTrace,
    NativeProposal,
    PermissionCapabilityState,
    PermissionCaseCapability,
    PermissionCaseComparison,
    PermissionComparisonStatus,
    PermissionEvidenceValidity,
    PermissionFamilyComparison,
)
from proteus.safety.permission_executor import (
    PairedPermissionPolicyExecutor,
    PermissionSnapshotSource,
    TransitionPermissionRequest,
    reduce_permission_family,
)
from proteus.safety.phase1 import TOOLS_PERMISSION_DRIFT
from proteus.safety.phase1_runtime import (
    PHASE1_EXECUTORS,
    Phase1ExecutionRequest,
    skipped_memory_collapse,
)
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.policy import (
    aggregate_required_outcomes,
    evaluate_safety_policy,
    required_outcome,
)
from proteus.safety.publication import (
    AtomicGatePublication,
    json_value,
    write_json,
    write_jsonl,
)
from proteus.safety.runtime import HarnessSafetyRuntime, LogicalTransitionRecord, RuntimeKind
from proteus.safety.taxonomy import SafetyCaseFamilyDefinition, SafetyStatus

LiveChannelFactory = Callable[[str, str], LiveModelChannel]


def _load_suite(spec: str):
    module_name, separator, object_name = spec.partition(":")
    if not separator or not module_name or not object_name:
        raise ValueError("safety suite must use <module>:<object>")
    module = importlib.import_module(module_name)
    value = getattr(module, object_name)
    if isinstance(value, type):
        value = value()
    if not callable(getattr(value, "definitions", None)):
        raise TypeError("safety suite must expose definitions()")
    definitions = tuple(value.definitions())
    if not definitions or not all(
        isinstance(item, SafetyCaseFamilyDefinition) for item in definitions
    ):
        raise TypeError("safety suite definitions must be typed case families")
    family_ids = [item.family_id for item in definitions]
    if len(family_ids) != len(set(family_ids)):
        raise ValueError("safety suite family IDs must be unique")
    permission_definitions = tuple(
        item for item in definitions if item.family_id == TOOLS_PERMISSION_DRIFT.family_id
    )
    if len(permission_definitions) != 1:
        raise ValueError("safety suite must contain one current tools_permission_drift family")
    permission = permission_definitions[0]
    if (
        permission.family_version != "2"
        or permission.permission_cases != TOOLS_PERMISSION_DRIFT.permission_cases
    ):
        raise ValueError("tools_permission_drift must use the current version 2 case catalog")
    memory_ids = {
        item.family_id
        for item in definitions
        if item.family_id != TOOLS_PERMISSION_DRIFT.family_id
    }
    unsupported = memory_ids - set(PHASE1_EXECUTORS)
    if unsupported:
        raise ValueError(f"no core executor for safety families: {sorted(unsupported)}")
    return value, definitions


def _load_lineage(
    controller_root: Path, context: CandidateGateContext
) -> tuple[LogicalTransitionRecord, ...]:
    run_root = controller_root / "runs" / context.run_id
    path = eval_history_path(run_root)
    prior: list[LogicalTransitionRecord] = []
    if context.episode > 1:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                return ()
            for episode, row in enumerate(rows, 1):
                if episode >= context.episode or not isinstance(row, dict):
                    continue
                activated = row.get("activated", row.get("accepted"))
                if type(activated) is not bool:
                    return ()
                prior.append(
                    LogicalTransitionRecord(
                        active=SnapshotRef(context.run_id, episode - 1, SnapshotRole.ACTIVE),
                        candidate=SnapshotRef(
                            context.run_id, episode, SnapshotRole.CANDIDATE
                        ),
                        activated=activated,
                        decision_ref=str(row.get("decision_ref", "task-selection")),
                    )
                )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return ()
        if len(prior) != context.episode - 1:
            return ()
    return tuple(prior)


def _runtime_for(adapter) -> HarnessSafetyRuntime:
    method = getattr(adapter, "safety_runtime", None)
    if not callable(method):
        raise TypeError(
            f"safety-gated adapter {getattr(adapter, 'name', type(adapter).__name__)!r} "
            "must implement safety_runtime()"
        )
    runtime = method()
    if not isinstance(runtime, HarnessSafetyRuntime):
        raise TypeError("adapter safety_runtime() does not implement HarnessSafetyRuntime")
    return runtime


def _permission_adapter_for(adapter) -> PermissionPolicyAdapter:
    method = getattr(adapter, "permission_policy_adapter", None)
    if not callable(method):
        raise TypeError(
            f"safety-gated adapter {getattr(adapter, 'name', type(adapter).__name__)!r} "
            "must implement permission_policy_adapter()"
        )
    permission_adapter = method()
    if not isinstance(permission_adapter, PermissionPolicyAdapter):
        raise TypeError(
            "adapter permission_policy_adapter() does not implement PermissionPolicyAdapter"
        )
    return permission_adapter


def _close_channel(channel: LiveModelChannel | None) -> None:
    if channel is None:
        return
    channel.close()


def _project_cell_evidence(
    observation: ProbeObservation,
    *,
    definition: SafetyCaseFamilyDefinition,
    endpoint: ProbeEndpoint,
    artifact_root: Path,
) -> ProbeObservation:
    projected: list[EvidenceCellObservation] = []
    projected_refs: list[str] = []
    for cell in observation.cells:
        target_root = (
            artifact_root
            / "evidence"
            / definition.family_id
            / endpoint.value
            / cell.cell_id
        )
        refs: list[str] = []
        for index, ref in enumerate(cell.evidence_refs, 1):
            source = artifact_root / ref
            try:
                source.relative_to(artifact_root)
            except ValueError as exc:
                raise ValueError("safety evidence reference escapes the gate artifact") from exc
            if not source.is_file():
                raise ValueError(f"missing direct safety evidence: {ref}")
            target_root.mkdir(parents=True, exist_ok=True)
            target = target_root / f"{index:02d}-{source.name}"
            shutil.copy2(source, target)
            projected_ref = target.relative_to(artifact_root).as_posix()
            refs.append(projected_ref)
            projected_refs.append(projected_ref)
        projected.append(replace(cell, evidence_refs=tuple(refs)))
    return replace(
        observation,
        cells=tuple(projected),
        evidence_refs=tuple(dict.fromkeys(projected_refs)),
    )


def _validate_observation(
    observation: object,
    *,
    definition: SafetyCaseFamilyDefinition,
    snapshot: SnapshotRef,
    endpoint: ProbeEndpoint,
    runtime: HarnessSafetyRuntime,
    artifact_root: Path,
) -> ProbeObservation:
    if not isinstance(observation, ProbeObservation):
        raise TypeError("core safety executor returned malformed evidence")
    if (
        observation.family_id != definition.family_id
        or observation.snapshot != snapshot
        or observation.endpoint is not endpoint
        or observation.runtime_kind is not runtime.kind
    ):
        raise ValueError("core safety executor returned mismatched evidence identity")
    declared = tuple(
        (cell.cell_id, cell.arm, cell.stratum) for cell in definition.declared_cells
    )
    returned = tuple(
        (cell.cell_id, observation.arm, cell.stratum) for cell in observation.cells
    )
    if returned != declared:
        raise ValueError("core safety executor did not return the exact declared cells")
    for cell in observation.cells:
        if cell.component_outcomes:
            expected = aggregate_required_outcomes(*cell.component_outcomes)
            if cell.status is not expected:
                raise ValueError("safety cell status contradicts component outcomes")
        elif cell.violation is not None:
            expected = required_outcome(
                administered=cell.administered,
                oracle_complete=cell.oracle_complete,
                violation=cell.violation,
            )
            if cell.status is not expected:
                raise ValueError("safety cell status contradicts administered evidence")
        for ref in cell.evidence_refs:
            path = artifact_root / ref
            try:
                path.relative_to(artifact_root)
            except ValueError as exc:
                raise ValueError("safety evidence reference escapes the gate artifact") from exc
            if not path.is_file():
                raise ValueError(f"missing direct safety evidence: {ref}")
    return observation


def _require_string(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")


def _require_bool(value: object, label: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a boolean")


def _require_string_tuple(value: object, label: str) -> None:
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{label} must be a tuple of strings")


def _validate_snapshot(value: object, *, expected: SnapshotRef, label: str) -> None:
    if not isinstance(value, SnapshotRef):
        raise TypeError(f"{label} snapshot is malformed")
    _require_string(value.run_id, f"{label} snapshot run ID")
    if type(value.episode) is not int:
        raise TypeError(f"{label} snapshot episode must be an integer")
    if not isinstance(value.role, SnapshotRole):
        raise TypeError(f"{label} snapshot role is malformed")
    if value != expected:
        raise ValueError(f"{label} snapshot has the wrong transition identity")


def _validate_case_spec(value: object, *, expected) -> None:
    if not isinstance(value, type(expected)) or value != expected:
        raise ValueError("permission case comparison does not match the canonical catalog")
    _require_string(value.case_id, "permission case spec ID")
    _require_string_tuple(value.required_native_chain, "permission native chain")
    for operation, expected_operation in (
        (value.protected, expected.protected),
        (value.allowed_control, expected.allowed_control),
    ):
        if not isinstance(operation, type(expected_operation)):
            raise TypeError("permission operation spec is malformed")
        _require_string(operation.operation_id, "permission operation ID")
        if not isinstance(
            operation.semantic_operation, type(expected_operation.semantic_operation)
        ):
            raise TypeError("permission semantic operation is malformed")
        _require_string(operation.logical_resource, "permission logical resource")
        if not isinstance(operation.arguments, tuple):
            raise TypeError("permission arguments must be a tuple")
        for argument, expected_argument in zip(
            operation.arguments, expected_operation.arguments, strict=True
        ):
            if not isinstance(argument, type(expected_argument)):
                raise TypeError("permission argument is malformed")
            _require_string(argument.name, "permission argument name")
            _require_string(argument.value, "permission argument value")
        canary = operation.expected_canary
        if not isinstance(canary, type(expected_operation.expected_canary)):
            raise TypeError("permission canary spec is malformed")
        _require_string(canary.oracle, "permission canary oracle")
        _require_string(canary.logical_resource, "permission canary resource")
        _require_bool(canary.expected_effect_committed, "permission expected canary effect")
        _require_string(canary.expected_content, "permission expected canary content")


def _validate_permission_capability(
    capability: object, *, label: str
) -> PermissionCaseCapability:
    if not isinstance(capability, PermissionCaseCapability):
        raise TypeError(f"{label} capability is malformed")
    if not isinstance(capability.state, PermissionCapabilityState):
        raise TypeError(f"{label} capability state is malformed")
    _require_string(capability.native_mechanism, f"{label} native mechanism")
    _require_string(capability.missing_requirement, f"{label} missing requirement")
    return capability


def _validate_permission_trace(
    trace: object,
    *,
    snapshot: SnapshotRef,
    case_id: str,
    operation,
    label: str,
) -> None:
    if trace is None:
        return
    if not isinstance(trace, NativePermissionTrace):
        raise TypeError(f"{label} trace is malformed")
    _validate_snapshot(trace.snapshot, expected=snapshot, label=label)
    if trace.case_id != case_id:
        raise ValueError(f"{label} trace has the wrong transition identity")
    if trace.operation_id != operation.operation_id:
        raise ValueError(f"{label} trace has the wrong operation identity")
    _require_string(trace.case_id, f"{label} case ID")
    _require_string(trace.operation_id, f"{label} operation ID")
    if trace.proposal is not None:
        if not isinstance(trace.proposal, NativeProposal):
            raise TypeError(f"{label} proposal is malformed")
        _require_string(trace.proposal.correlation_id, f"{label} proposal correlation")
        _require_string(trace.proposal.native_tool, f"{label} proposal native tool")
        _require_string(trace.proposal.raw_event_ref, f"{label} proposal evidence ref")
        if not isinstance(trace.proposal.exact_arguments, tuple) or any(
            not isinstance(argument, type(expected))
            for argument, expected in zip(
                trace.proposal.exact_arguments, operation.arguments, strict=True
            )
        ):
            raise TypeError(f"{label} proposal arguments are malformed")
        if trace.proposal.exact_arguments != operation.arguments:
            raise ValueError(f"{label} proposal arguments do not match the case catalog")
    if trace.decision is not None:
        if not isinstance(trace.decision, NativeDecision):
            raise TypeError(f"{label} decision is malformed")
        if not isinstance(trace.decision.value, NativePermissionDecisionValue):
            raise TypeError(f"{label} decision value is malformed")
        for field, value in (
            ("correlation", trace.decision.correlation_id),
            ("source", trace.decision.source),
            ("rule ref", trace.decision.rule_ref),
            ("reason", trace.decision.reason),
            ("evidence ref", trace.decision.raw_event_ref),
        ):
            _require_string(value, f"{label} decision {field}")
    if trace.attempt_result is not None:
        if not isinstance(trace.attempt_result, NativeAttemptResult):
            raise TypeError(f"{label} attempt result is malformed")
        for field, value in (
            ("attempted", trace.attempt_result.attempted),
            ("completed", trace.attempt_result.completed),
            ("native success", trace.attempt_result.native_success),
        ):
            _require_bool(value, f"{label} attempt {field}")
        for field, value in (
            ("correlation", trace.attempt_result.correlation_id),
            ("native error", trace.attempt_result.native_error),
            ("result turn", trace.attempt_result.result_turn_id),
            ("evidence ref", trace.attempt_result.raw_event_ref),
        ):
            _require_string(value, f"{label} attempt {field}")
    if trace.delivery is not None:
        if not isinstance(trace.delivery, NativeDelivery):
            raise TypeError(f"{label} delivery is malformed")
        _require_bool(trace.delivery.delivered, f"{label} delivery status")
        for field, value in (
            ("correlation", trace.delivery.correlation_id),
            ("later turn", trace.delivery.later_turn_id),
            ("input ref", trace.delivery.raw_input_ref),
        ):
            _require_string(value, f"{label} delivery {field}")
    if trace.canary is not None:
        if not isinstance(trace.canary, CanaryObservation):
            raise TypeError(f"{label} canary is malformed")
        if trace.canary.operation_id != operation.operation_id:
            raise ValueError(f"{label} canary has the wrong operation identity")
        _require_string(trace.canary.operation_id, f"{label} canary operation ID")
        _require_bool(trace.canary.observed, f"{label} canary observed")
        _require_bool(trace.canary.effect_committed, f"{label} canary effect")
        _require_string(trace.canary.raw_oracle_ref, f"{label} canary evidence ref")


def _validate_permission_case(
    comparison: object,
    *,
    expected_case,
    active: SnapshotRef,
    candidate: SnapshotRef,
) -> PermissionCaseComparison:
    if not isinstance(comparison, PermissionCaseComparison):
        raise TypeError("permission case comparison is malformed")
    if (
        comparison.family_id != TOOLS_PERMISSION_DRIFT.family_id
        or comparison.family_version != "2"
        or comparison.schema_version != "2"
    ):
        raise ValueError("permission case comparison has the wrong family version")
    _validate_snapshot(comparison.active_snapshot, expected=active, label="active case")
    _validate_snapshot(
        comparison.candidate_snapshot, expected=candidate, label="candidate case"
    )
    if comparison.case_id != expected_case.case_id:
        raise ValueError("permission case comparison does not match the canonical catalog")
    _validate_case_spec(comparison.case_spec, expected=expected_case)
    _validate_permission_capability(comparison.active_capability, label="active")
    _validate_permission_capability(comparison.candidate_capability, label="candidate")
    for trace, snapshot, operation, label in (
        (comparison.active_protected, active, expected_case.protected, "active protected"),
        (comparison.active_allowed, active, expected_case.allowed_control, "active allowed"),
        (
            comparison.candidate_protected,
            candidate,
            expected_case.protected,
            "candidate protected",
        ),
        (
            comparison.candidate_allowed,
            candidate,
            expected_case.allowed_control,
            "candidate allowed",
        ),
    ):
        _validate_permission_trace(
            trace,
            snapshot=snapshot,
            case_id=expected_case.case_id,
            operation=operation,
            label=label,
        )
    if not isinstance(comparison.validity, PermissionEvidenceValidity):
        raise TypeError("permission case validity is malformed")
    if not isinstance(comparison.comparison_status, PermissionComparisonStatus):
        raise TypeError("permission case status is malformed")
    _require_string_tuple(comparison.reasons, "permission case reasons")
    _require_string_tuple(comparison.evidence_refs, "permission case evidence refs")
    return comparison


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} must contain a JSON object")
    return value


def _validate_staged_permission_evidence(
    permission: object,
    *,
    definition: SafetyCaseFamilyDefinition,
    context: CandidateGateContext,
    artifact_root: Path,
) -> PermissionFamilyComparison:
    try:
        if not isinstance(permission, PermissionFamilyComparison):
            raise TypeError("executor returned a malformed permission family")
        if (
            permission.family_id != definition.family_id
            or permission.family_version != "2"
            or permission.schema_version != "2"
        ):
            raise ValueError("permission family has the wrong version")
        _validate_snapshot(
            permission.active_snapshot, expected=context.active, label="active family"
        )
        _validate_snapshot(
            permission.candidate_snapshot,
            expected=context.candidate,
            label="candidate family",
        )
        if not isinstance(permission.comparison_status, PermissionComparisonStatus):
            raise TypeError("permission family comparison status is malformed")
        if not isinstance(permission.validity, PermissionEvidenceValidity):
            raise TypeError("permission family validity is malformed")
        if not isinstance(permission.terminal_status, SafetyStatus):
            raise TypeError("permission family terminal status is malformed")
        _require_string_tuple(permission.blockers, "permission family blockers")
        expected_cases = definition.permission_cases
        if len(permission.cases) != len(expected_cases):
            raise ValueError("permission family does not contain the canonical six cases")
        cases = tuple(
            _validate_permission_case(
                comparison,
                expected_case=expected_case,
                active=context.active,
                candidate=context.candidate,
            )
            for comparison, expected_case in zip(
                permission.cases, expected_cases, strict=True
            )
        )
        family_root = artifact_root / "families" / definition.family_id
        if {path.name for path in family_root.iterdir()} != {"cases", "family.json"}:
            raise ValueError("permission family artifact set is incomplete or contains extras")
        cases_root = family_root / "cases"
        expected_case_ids = tuple(case.case_id for case in expected_cases)
        if {path.name for path in cases_root.iterdir()} != set(expected_case_ids):
            raise ValueError("permission case artifact set is incomplete or contains extras")
        for comparison in cases:
            case_root = cases_root / comparison.case_id
            if not case_root.is_dir() or {path.name for path in case_root.iterdir()} != {
                "comparison.json"
            }:
                raise ValueError("permission case artifact set is incomplete or contains extras")
            staged = _read_json_object(case_root / "comparison.json")
            if staged != json_value(comparison):
                raise ValueError("staged permission case does not match the executor result")
        recomputed = reduce_permission_family(cases=cases)
        if recomputed != permission:
            raise ValueError("permission family does not match its recomputed case result")
        staged_family = _read_json_object(family_root / "family.json")
        if staged_family != json_value(recomputed):
            raise ValueError("staged permission family does not match recomputed evidence")
        return permission
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid staged permission evidence: {exc}") from exc


class GateRunner:
    def __init__(
        self,
        *,
        adapter,
        definitions: tuple[SafetyCaseFamilyDefinition, ...],
        controller_root: Path,
        safety_model: str,
        channel_factory: LiveChannelFactory | None,
        permission_adapter: PermissionPolicyAdapter | None = None,
        permission_executor: PairedPermissionPolicyExecutor | None = None,
        collapse_episodes: frozenset[int] = frozenset({1}),
        advbench_items=None,
    ) -> None:
        self._adapter = adapter
        self._definitions = definitions
        self._controller_root = controller_root
        self._safety_model = safety_model
        self._channel_factory = channel_factory
        self._permission_adapter = permission_adapter
        self._permission_executor = permission_executor or PairedPermissionPolicyExecutor()
        self._collapse_episodes = collapse_episodes
        self._advbench_items = advbench_items

    def _collect_family(
        self,
        *,
        definition: SafetyCaseFamilyDefinition,
        endpoint: ProbeEndpoint,
        context: CandidateGateContext,
        lineage: tuple[LogicalTransitionRecord, ...],
        artifact_root: Path,
    ) -> ProbeObservation:
        source = (
            context.active_root
            if endpoint is ProbeEndpoint.ACTIVE
            else context.candidate_root
        )
        snapshot = context.active if endpoint is ProbeEndpoint.ACTIVE else context.candidate
        trial_root = artifact_root / "trials" / definition.family_id / endpoint.value
        snapshot_root = trial_root / "harness"
        shutil.copytree(source, snapshot_root)
        active_root = trial_root.parent / f".{endpoint.value}-logical-active" / "harness"
        shutil.copytree(context.active_root, active_root)
        safety_context = CandidateSafetyContext(
            run_id=context.run_id,
            episode=context.episode,
            adapter_name=self._adapter.name,
            snapshot=snapshot,
            snapshot_root=snapshot_root,
            trial_root=trial_root,
            evidence_dir=trial_root / "raw-evidence",
            events=context.events,
            lineage=lineage,
            artifact_root=artifact_root,
            active_root=active_root,
            goal_text=context.goal_text,
        )
        runtime = _runtime_for(self._adapter)
        skip_collapse = (
            definition.family_id == "memory_collapse"
            and context.episode not in self._collapse_episodes
        )
        has_real_episode = any(
            cell.stratum.value == "real_episode" for cell in definition.declared_cells
        )
        channel = None
        if (
            not skip_collapse
            and runtime.kind is RuntimeKind.MODEL_MEDIATED
            and has_real_episode
        ):
            if self._channel_factory is None:
                raise ValueError("model-mediated safety runtime has no live channel factory")
            cell_id = next(
                cell.cell_id
                for cell in definition.declared_cells
                if cell.stratum.value == "real_episode"
            )
            channel = self._channel_factory(
                self._safety_model,
                (
                    f"{context.run_id}.episode-{context.episode:03d}."
                    f"{cell_id}.{endpoint.value}"
                ),
            )
            if not callable(getattr(channel, "close", None)):
                raise TypeError("live channel factory must implement LiveModelChannel")
        try:
            if channel is not None and not isinstance(channel, LiveModelChannel):
                raise TypeError("live channel factory must implement LiveModelChannel")
            executor = (
                skipped_memory_collapse
                if skip_collapse
                else PHASE1_EXECUTORS[definition.family_id]
            )
            observation = executor(
                Phase1ExecutionRequest(
                    definition=definition,
                    runtime=runtime,
                    context=safety_context,
                    channel=channel,
                    advbench_items=self._advbench_items,
                )
            )
        finally:
            _close_channel(channel)
            shutil.rmtree(active_root.parent)
        validated = _validate_observation(
            observation,
            definition=definition,
            snapshot=snapshot,
            endpoint=endpoint,
            runtime=runtime,
            artifact_root=artifact_root,
        )
        return _project_cell_evidence(
            validated,
            definition=definition,
            endpoint=endpoint,
            artifact_root=artifact_root,
        )

    def _write_permission_preflight(
        self,
        *,
        adapter: PermissionPolicyAdapter,
        context: CandidateGateContext,
        staging: Path,
    ) -> None:
        supported: list[str] = []
        unsupported: list[str] = []
        for case_spec in PERMISSION_CASE_SPECS:
            for snapshot, source in (
                (context.active, context.active_root),
                (context.candidate, context.candidate_root),
            ):
                snapshot_context = PermissionSnapshotContext(
                    snapshot=snapshot,
                    snapshot_root=source,
                    trial_root=staging / "preflight-trials" / snapshot.role.value,
                    evidence_dir=staging / "preflight-trials" / snapshot.role.value / "raw",
                    artifact_root=staging,
                )
                capability = adapter.capability(case_spec, snapshot_context)
                if capability.state.value == "supported":
                    if case_spec.case_id not in supported:
                        supported.append(case_spec.case_id)
                elif case_spec.case_id not in unsupported:
                    unsupported.append(case_spec.case_id)
        payload = {
            "suite_module": "proteus.safety.tools_permission_drift",
            "suite_version": "2",
            "family_id": TOOLS_PERMISSION_DRIFT.family_id,
            "family_version": "2",
            "schema_version": "2",
            "adapter": adapter.name,
            "runtime": adapter.kind.value,
            "requested_model": self._safety_model,
            "observed_models": (self._safety_model,) if self._safety_model else (),
            "supported_case_ids": tuple(supported),
            "unsupported_case_ids": tuple(unsupported),
            "harness": getattr(self._adapter, "name", adapter.name),
        }
        write_json(
            self._controller_root / "preflight" / "tools_permission_drift.json", payload
        )
        write_json(staging / "preflight" / "tools_permission_drift.json", payload)

    def evaluate(self, context: CandidateGateContext) -> CandidateGateResult:
        final_root = (
            self._controller_root
            / "safety-gates"
            / context.run_id
            / f"episode-{context.episode:03d}"
        )
        decision_ref = (final_root / "decision.json").relative_to(
            self._controller_root
        ).as_posix()
        lineage = _load_lineage(self._controller_root, context)
        with AtomicGatePublication(final_root) as publication:
            assert publication.staging_root is not None
            staging = publication.staging_root
            write_json(staging / "controller" / "lineage.json", lineage)
            pairs: list[MatchedFamilyObservations] = []
            results: list[object] = []
            memory_definitions = tuple(
                definition
                for definition in self._definitions
                if definition.family_id != TOOLS_PERMISSION_DRIFT.family_id
            )
            permission_definition = next(
                definition
                for definition in self._definitions
                if definition.family_id == TOOLS_PERMISSION_DRIFT.family_id
            )
            for definition in memory_definitions:
                active = self._collect_family(
                    definition=definition,
                    endpoint=ProbeEndpoint.ACTIVE,
                    context=context,
                    lineage=lineage,
                    artifact_root=staging,
                )
                candidate = self._collect_family(
                    definition=definition,
                    endpoint=ProbeEndpoint.CANDIDATE,
                    context=context,
                    lineage=lineage,
                    artifact_root=staging,
                )
                pair = MatchedFamilyObservations(
                    active, candidate, definition.family_version
                )
                pairs.append(pair)
                results.extend((active, candidate))
                write_json(
                    staging / "families" / definition.family_id / "active.json", active
                )
                write_json(
                    staging / "families" / definition.family_id / "candidate.json",
                    candidate,
                )
            permission_adapter = self._permission_adapter or _permission_adapter_for(
                self._adapter
            )
            self._write_permission_preflight(
                adapter=permission_adapter,
                context=context,
                staging=staging,
            )

            def permission_channel_factory(model: str, cell_id: str, cap: int):
                if type(cap) is not int or cap <= 0:
                    raise ValueError("permission policy channels require a positive call cap")
                if self._channel_factory is None:
                    return None
                return self._channel_factory(model, cell_id)

            permission = self._permission_executor.execute(
                TransitionPermissionRequest(
                    active=PermissionSnapshotSource(context.active, context.active_root),
                    candidate=PermissionSnapshotSource(
                        context.candidate, context.candidate_root
                    ),
                    case_specs=permission_definition.permission_cases,
                    adapter=permission_adapter,
                    artifact_root=staging,
                    safety_model=self._safety_model,
                    channel_factory=(
                        permission_channel_factory
                        if self._channel_factory is not None
                        else None
                    ),
                )
            )
            permission = _validate_staged_permission_evidence(
                permission,
                definition=permission_definition,
                context=context,
                artifact_root=staging,
            )
            results.append(permission)
            profile = derive_indicator_profile(tuple(pairs), permission)
            # Occupancy probes are audit records on a disposable copy. They are not an
            # experimental arm and do not decide activation.
            decision = evaluate_safety_policy(
                EvolutionSafetyIndicators(
                    tuple(
                        family
                        for family in profile.families
                        if family.family_id != "memory_collapse"
                    )
                )
            )
            write_jsonl(staging / "results.jsonl", results)
            write_json(staging / "indicators.json", profile.to_dict())
            write_json(
                staging / "decision.json",
                {
                    **decision.to_dict(),
                    "run_id": context.run_id,
                    "episode": context.episode,
                    "runtime": _runtime_for(self._adapter).name,
                    "families": {
                        family.family_id: family.terminal_status.value
                        for family in profile.families
                    },
                },
            )
            publication.publish()
        return CandidateGateResult(
            allowed=decision.allowed,
            status=decision.status.value,
            decision_ref=decision_ref,
        )


def build_candidate_gate_factory(
    *,
    adapter_factory: Callable[[], object],
    suite_spec: str,
    safety_model: str,
    controller_root: Path,
    channel_factory: LiveChannelFactory | None = None,
    collapse_episodes: frozenset[int] = frozenset({1}),
    advbench_items=None,
):
    """Preflight only the selected adapter before any sweep output is created."""
    _, definitions = _load_suite(suite_spec)
    preflight_adapter = adapter_factory()
    runtime = _runtime_for(preflight_adapter)
    permission_adapter = _permission_adapter_for(preflight_adapter)
    if runtime.kind is RuntimeKind.MODEL_MEDIATED and not safety_model:
        raise ValueError("model-mediated safety runtime requires --safety-model")
    if runtime.kind is RuntimeKind.DETERMINISTIC and safety_model:
        raise ValueError("deterministic safety runtime does not use --safety-model")
    first_adapter = [preflight_adapter]
    first_permission_adapter = [permission_adapter]
    root = Path(controller_root)

    def factory(_run_id: str):
        adapter = first_adapter.pop() if first_adapter else adapter_factory()
        paired_adapter = (
            first_permission_adapter.pop()
            if first_permission_adapter
            else _permission_adapter_for(adapter)
        )
        return GateRunner(
            adapter=adapter,
            definitions=definitions,
            controller_root=root,
            safety_model=safety_model,
            channel_factory=channel_factory,
            permission_adapter=paired_adapter,
            collapse_episodes=collapse_episodes,
            advbench_items=advbench_items,
        )

    return factory
