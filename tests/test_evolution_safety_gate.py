from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from threading import Barrier, current_thread

import pytest
from safety_memory_fixtures import plant_harness_memory, synthetic_advbench

from proteus import cli
from proteus.adapters.llm import LLMHarness
from proteus.adapters.minimal import MinimalHarness
from proteus.adapters.minimal_safety import MinimalSafetyRuntime
from proteus.adapters.pi import PiHarness
from proteus.core.activation import SettledEpisodeSafetyContext
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import EvidenceCellObservation
from proteus.safety.gate import (
    PostEpisodeSafetyRunner,
    _episode_audit_status,
    build_safety_runner_factory,
)
from proteus.safety.indicators import (
    EpisodeSafetyIndicators,
    FamilyExecution,
    PermissionCaseIndicators,
    PermissionCaseState,
    ToolsPermissionIndicators,
    empty_admission,
    empty_collapse,
    empty_permission,
    not_scheduled_execution,
)
from proteus.safety.live import LiveCallProvenance
from proteus.safety.permission_adapter import PermissionSnapshotContext
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS
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
    PermissionEvidenceValidity,
)
from proteus.safety.permission_executor import (
    NativeCallableInventoryEvidence,
    PermissionSnapshotSource,
    SnapshotPermissionExecutor,
    SnapshotPermissionRequest,
)
from proteus.safety.phase1 import SUITE, TOOLS_PERMISSION_DRIFT
from proteus.safety.runtime import RuntimeKind
from proteus.safety.schedule import EveryEpisode, ExplicitEpisodes, SafetySuiteSchedule
from proteus.safety.taxonomy import EvidenceStratum, SafetyStatus
from proteus.safety.tool_catalog import (
    AdapterOwnedToolCoverage,
    NativeToolCatalog,
    NativeToolSchema,
)

_TEXT_DISPATCH_SUPPORTED = frozenset({"protected_overwrite", "workspace_boundary"})


class RecordingMinimalSafetyRuntime(MinimalSafetyRuntime):
    def __init__(self, harness) -> None:
        super().__init__(harness)
        self.memory_endpoint_calls: set[tuple[str, str]] = set()

    def introduce_memory(self, request, context):
        family_id = (
            "memory_collapse"
            if request.state_id.startswith(("collapse-anchor-", "corpus-"))
            else "memory_bad_admission"
        )
        self.memory_endpoint_calls.add((family_id, context.snapshot.role.value))
        return super().introduce_memory(request, context)


class GatePermissionAdapter:
    name = "gate-fixture-permission"
    kind = RuntimeKind.DETERMINISTIC
    permission_requires_live_channel = True
    declared_supported_case_ids = frozenset(
        case.case_id for case in TOOLS_PERMISSION_DRIFT.permission_cases
    )

    @staticmethod
    def live_call_cap(case_spec):
        del case_spec
        return 2

    @staticmethod
    def capability(case_spec, snapshot_context):
        del case_spec, snapshot_context
        return PermissionCaseCapability(
            PermissionCapabilityState.SUPPORTED, "fixture-native-policy", ""
        )

    @staticmethod
    def bind(case_spec, snapshot_context):
        def operation_binding(operation):
            return NativeOperationBinding(
                operation.operation_id,
                operation.semantic_operation,
                operation.logical_resource,
                "fixture.native_tool",
                operation.arguments,
                "raw",
            )

        snap = snapshot_context.snapshot
        return NativePermissionBinding(
            case_spec.case_id,
            f"fixture-native-policy:{snap.run_id}:{snap.episode}:{snap.role.value}",
            operation_binding(case_spec.protected),
            operation_binding(case_spec.allowed_control),
        )

    @staticmethod
    def administer(binding, operation_spec, channel):
        _prefix, run_id, episode, role = binding.native_mechanism.split(":", 3)
        snapshot = SnapshotRef(run_id, int(episode), SnapshotRole(role))
        allowed = operation_spec.operation_id == binding.allowed_control.operation_id
        correlation = f"{snapshot.role.value}-{operation_spec.operation_id}"
        if channel is not None and hasattr(channel, "requests"):
            channel.respond(
                input=f"administer {operation_spec.operation_id}",
                instructions="return only the native operation result",
            )
        return NativePermissionTrace(
            snapshot=snapshot,
            case_id=binding.case_id,
            operation_id=operation_spec.operation_id,
            proposal=NativeProposal(
                correlation,
                "fixture.native_tool",
                operation_spec.arguments,
                "raw/proposal.json",
            ),
            decision=NativeDecision(
                correlation,
                (
                    NativePermissionDecisionValue.ALLOW
                    if allowed
                    else NativePermissionDecisionValue.DENY
                ),
                "fixture-native-policy",
                "fixture/rule",
                "matched fixture decision",
                "raw/decision.json",
            ),
            attempt_result=NativeAttemptResult(
                correlation,
                attempted=True,
                completed=True,
                native_success=allowed,
                native_error="" if allowed else "denied",
                result_turn_id="turn-1",
                raw_event_ref="raw/result.json",
            ),
            delivery=NativeDelivery(
                correlation, True, "turn-2", "raw/delivery.json"
            ),
            canary=None,
        )

    @staticmethod
    def observe_canary(binding, operation_spec):
        return CanaryObservation(
            operation_spec.operation_id,
            observed=True,
            effect_committed=(
                operation_spec.operation_id == binding.allowed_control.operation_id
            ),
            raw_oracle_ref="raw/canary.json",
        )

    @staticmethod
    def verify_native_callable_inventory(binding, operation_spec, snapshot_context):
        return NativeCallableInventoryEvidence(
            snapshot=snapshot_context.snapshot,
            operation_id=operation_spec.operation_id,
            fresh_runtime_id="fixture-fresh-native-runtime",
            catalog_ref="raw/callable-inventory.json",
            callable_name="fixture.native_tool",
            registered=True,
            invocation_attempted=True,
            invocation_succeeded=True,
            invocation_result_ref="raw/callable-invocation.json",
        )


class GateFixtureAdapter(MinimalHarness):
    name = "gate-fixture"

    def __init__(self) -> None:
        super().__init__()
        self.runtime = RecordingMinimalSafetyRuntime(self)
        self.permission_adapter = GatePermissionAdapter()

    @property
    def memory_endpoint_calls(self) -> set[tuple[str, str]]:
        return self.runtime.memory_endpoint_calls

    def safety_runtime(self):
        return self.runtime

    def permission_policy_adapter(self):
        return self.permission_adapter


class DshReadinessPermissionAdapter(GatePermissionAdapter):
    """Fixed native-pair fixture for the DSH readiness gate path."""

    name = "dsh"
    permission_requires_live_channel = False
    declared_supported_case_ids = frozenset(
        case.case_id
        for case in PERMISSION_CASE_SPECS
        if case.case_id
        in {"recursive_deletion", "protected_overwrite", "workspace_boundary"}
    )

    def __init__(self, *, allowed_effect: bool = True) -> None:
        self.allowed_effect = allowed_effect

    @staticmethod
    def live_call_cap(case_spec):
        del case_spec
        return 0

    def capability(self, case_spec, snapshot_context):
        del snapshot_context
        if case_spec.case_id not in self.declared_supported_case_ids:
            return PermissionCaseCapability(
                PermissionCapabilityState.UNSUPPORTED,
                "",
                "native_dsh_permission_boundary_unavailable",
            )
        return PermissionCaseCapability(
            PermissionCapabilityState.SUPPORTED,
            "dsh.fixture.native-sandbox-policy",
            "",
        )

    def observe_canary(self, binding, operation_spec):
        allowed = operation_spec.operation_id == binding.allowed_control.operation_id
        return CanaryObservation(
            operation_spec.operation_id,
            observed=True,
            effect_committed=allowed and self.allowed_effect,
            raw_oracle_ref="raw/canary.json",
        )


class DshReadinessHarness(GateFixtureAdapter):
    name = "dsh"

    def __init__(self, *, allowed_effect: bool = True) -> None:
        super().__init__()
        self.permission_adapter = DshReadinessPermissionAdapter(
            allowed_effect=allowed_effect
        )


def _fixture_native_tool_schema(name: str, version: str = "1") -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"fixture tool {name}",
            "parameters": {
                "type": "object",
                "properties": {"version": {"const": version}},
            },
        },
    }


class CatalogGatePermissionAdapter(GatePermissionAdapter):
    """Fixture adapter that records only model-offered tool schemas."""

    def __init__(self, catalogs: dict[int, tuple[dict[str, object], ...]]) -> None:
        self.catalogs = catalogs
        self.catalog_calls = 0

    def collect_native_tool_catalog(self, context: PermissionSnapshotContext):
        self.catalog_calls += 1
        schemas = self.catalogs.get(context.snapshot.episode)
        if schemas is None:
            return None
        raw_path = context.evidence_dir / "fixture-catalog.json"
        raw_path.write_text(json.dumps({"tools": schemas}), encoding="utf-8")
        raw_ref = raw_path.relative_to(context.artifact_root).as_posix()
        return NativeToolCatalog(
            snapshot=context.snapshot,
            loader_id="fixture.native_tool_loader",
            tools=tuple(
                sorted(
                    (
                        NativeToolSchema.from_schema(
                            name=schema["function"]["name"],  # type: ignore[index]
                            schema=schema,
                            raw_schema_ref=raw_ref,
                        )
                        for schema in schemas
                    ),
                    key=lambda tool: tool.name,
                )
            ),
            raw_catalog_ref=raw_ref,
        )

    @staticmethod
    def native_tool_catalog_reason(_snapshot: SnapshotRef) -> str:
        return "fixture_native_tool_catalog_missing"


class CatalogGateFixtureAdapter(GateFixtureAdapter):
    def __init__(self, catalogs: dict[int, tuple[dict[str, object], ...]]) -> None:
        super().__init__()
        self.permission_adapter = CatalogGatePermissionAdapter(catalogs)


def _catalog_gate_context(
    tmp_path: Path,
    *,
    episode: int,
    previous: SettledEpisodeSafetyContext | None = None,
) -> SettledEpisodeSafetyContext:
    snapshot_root = tmp_path / "catalog-subject" / f"episode-{episode:03d}"
    MinimalHarness().seed(snapshot_root)
    plant_harness_memory(snapshot_root)
    return SettledEpisodeSafetyContext(
        run_id="catalog-run",
        episode=episode,
        snapshot_ref=SnapshotRef("catalog-run", episode, SnapshotRole.ACTIVE),
        snapshot_root=snapshot_root,
        trace=(),
        episodes_target=2,
        previous_snapshot_ref=(previous.snapshot_ref if previous is not None else None),
        previous_snapshot_root=(previous.snapshot_root if previous is not None else None),
    )


class RecordingChannel:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    @property
    def model(self) -> str:
        return "fixture-model"

    def respond(self, **request):
        self.requests.append(request)
        return object()

    def close(self) -> None:
        pass


class ExplodingAfterCaseExecutor(SnapshotPermissionExecutor):
    def __init__(self, *, case_number: int) -> None:
        self.case_number = case_number

    def execute(self, request):
        for case_spec in request.case_specs[: self.case_number]:
            path = (
                request.artifact_root
                / "tools_permission_drift"
                / "cases"
                / case_spec.case_id
                / "result.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        raise RuntimeError("controlled case failure")


class RecordingSnapshotPermissionExecutor(SnapshotPermissionExecutor):
    def __init__(self, mutation=None) -> None:
        self.execute_calls = 0
        self.mutation = mutation

    def execute(self, request):
        self.execute_calls += 1
        result = super().execute(request)
        return self.mutation(request, result) if self.mutation is not None else result


def _every_family_schedule() -> SafetySuiteSchedule:
    return SafetySuiteSchedule(
        memory_bad_admission=EveryEpisode(),
        memory_collapse=EveryEpisode(),
        tools_permission_drift=EveryEpisode(),
    )


def _gate_context(tmp_path: Path) -> SettledEpisodeSafetyContext:
    snapshot_root = tmp_path / "subject" / "settled"
    MinimalHarness().seed(snapshot_root)
    plant_harness_memory(snapshot_root)
    return SettledEpisodeSafetyContext(
        run_id="matched-run",
        episode=1,
        snapshot_ref=SnapshotRef("matched-run", 1, SnapshotRole.ACTIVE),
        snapshot_root=snapshot_root,
        trace=(),
        episodes_target=1,
        snapshot_commit="commit-1",
    )


def _permission_snapshot_context(tmp_path: Path) -> PermissionSnapshotContext:
    snapshot_root = tmp_path / "permission-snapshot"
    snapshot_root.mkdir()
    return PermissionSnapshotContext(
        snapshot=SnapshotRef("permission-run", 1, SnapshotRole.CANDIDATE),
        snapshot_root=snapshot_root,
        trial_root=tmp_path / "permission-trial",
        evidence_dir=tmp_path / "permission-evidence",
        artifact_root=tmp_path,
    )


def test_dsh_permission_readiness_uses_only_declared_cases_and_publishes_artifacts(
    tmp_path: Path,
) -> None:
    adapter = DshReadinessHarness(allowed_effect=False)
    configured_permission_adapter = DshReadinessPermissionAdapter(allowed_effect=True)
    executor = RecordingSnapshotPermissionExecutor()
    opened: list[str] = []
    gate = PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=(TOOLS_PERMISSION_DRIFT,),
        controller_root=tmp_path / "controller",
        safety_model="gpt-5.6-luna",
        channel_factory=lambda *_args: opened.append("provider") or pytest.fail(
            "DSH readiness must not open a provider channel"
        ),
        permission_adapter=configured_permission_adapter,
        permission_executor=executor,
    )
    snapshot_root = tmp_path / "dsh-seed"
    MinimalHarness().seed(snapshot_root)

    gate.preflight_permission_measurement(
        run_id="dsh-readiness",
        snapshot_root=snapshot_root,
        episode=0,
    )

    root = (
        tmp_path
        / "controller/preflight/runs/dsh-readiness/permission-readiness/episode-000"
    )
    manifest = json.loads((root / "readiness.json").read_text(encoding="utf-8"))
    result = json.loads(
        (root / "tools_permission_drift/result.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "ready"
    assert manifest["supported_case_ids"] == [
        "recursive_deletion",
        "protected_overwrite",
        "workspace_boundary",
    ]
    assert [case["case_id"] for case in result["cases"]] == manifest[
        "supported_case_ids"
    ]
    assert all(case["ready"] is True for case in manifest["cases"])
    assert executor.execute_calls == 1
    assert opened == []
    assert not (
        root / "tools_permission_drift/cases/policy_mutation/result.json"
    ).exists()


def test_dsh_permission_readiness_stops_on_missing_allowed_effect(tmp_path: Path) -> None:
    class MissingAllowedCanaryAdapter(DshReadinessPermissionAdapter):
        @staticmethod
        def observe_canary(binding, operation_spec):
            allowed = operation_spec.operation_id == binding.allowed_control.operation_id
            return CanaryObservation(
                operation_spec.operation_id,
                observed=not allowed,
                effect_committed=False,
                raw_oracle_ref="raw/canary.json",
            )

    adapter = DshReadinessHarness()
    permission_adapter = MissingAllowedCanaryAdapter()
    gate = PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=(TOOLS_PERMISSION_DRIFT,),
        controller_root=tmp_path / "controller",
        safety_model="gpt-5.6-luna",
        channel_factory=lambda *_args: pytest.fail(
            "DSH readiness must not open a provider channel"
        ),
        permission_adapter=permission_adapter,
    )
    snapshot_root = tmp_path / "dsh-seed"
    MinimalHarness().seed(snapshot_root)

    with pytest.raises(
        RuntimeError,
        match="DSH permission readiness failed before safety baseline",
    ):
        gate.preflight_permission_measurement(
            run_id="dsh-unready",
            snapshot_root=snapshot_root,
            episode=0,
        )

    manifest = json.loads(
        (
            tmp_path
            / "controller/preflight/runs/dsh-unready/permission-readiness/episode-000"
            / "readiness.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "not_ready"
    assert all(case["allowed_effect_committed"] is None for case in manifest["cases"])


def test_dsh_permission_readiness_accepts_complete_unsafe_native_evidence(
    tmp_path: Path,
) -> None:
    class UnsafeDshReadinessPermissionAdapter(DshReadinessPermissionAdapter):
        @staticmethod
        def administer(binding, operation_spec, channel):
            trace = GatePermissionAdapter.administer(binding, operation_spec, channel)
            if operation_spec.operation_id != binding.protected.operation_id:
                return trace
            assert trace.decision is not None
            assert trace.attempt_result is not None
            return replace(
                trace,
                decision=replace(
                    trace.decision,
                    value=NativePermissionDecisionValue.ALLOW,
                    reason="fixture permits protected operation",
                ),
                attempt_result=replace(
                    trace.attempt_result,
                    native_success=True,
                    native_error="",
                ),
            )

        @staticmethod
        def observe_canary(binding, operation_spec):
            return CanaryObservation(
                operation_spec.operation_id,
                observed=True,
                effect_committed=True,
                raw_oracle_ref="raw/canary.json",
            )

    adapter = DshReadinessHarness()
    permission_adapter = UnsafeDshReadinessPermissionAdapter()
    gate = PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=(TOOLS_PERMISSION_DRIFT,),
        controller_root=tmp_path / "controller",
        safety_model="gpt-5.6-luna",
        channel_factory=lambda *_args: pytest.fail(
            "DSH readiness must not open a provider channel"
        ),
        permission_adapter=permission_adapter,
    )
    snapshot_root = tmp_path / "dsh-seed"
    MinimalHarness().seed(snapshot_root)

    gate.preflight_permission_measurement(
        run_id="dsh-unsafe-but-measurable",
        snapshot_root=snapshot_root,
        episode=0,
    )

    manifest = json.loads(
        (
            tmp_path
            / "controller/preflight/runs/dsh-unsafe-but-measurable/"
            "permission-readiness/episode-000/readiness.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["status"] == "ready"
    assert all(case["ready"] is True for case in manifest["cases"])
    assert all(case["observed_safe_pair"] is False for case in manifest["cases"])


@pytest.mark.parametrize("harness_name", ("dsh", "pi"))
def test_compatible_builtin_family_measurements_overlap_then_return_in_fixed_order(
    tmp_path: Path,
    harness_name: str,
) -> None:
    class ParallelRunner(PostEpisodeSafetyRunner):
        def __init__(self) -> None:
            adapter = DshReadinessHarness()
            super().__init__(
                adapter=adapter,
                definitions=SUITE.definitions(),
                controller_root=tmp_path / "controller",
                safety_model="",
                channel_factory=None,
            )
            self.barrier = Barrier(3)
            self.worker_names: set[str] = set()

        def synchronized(self, result):
            self.worker_names.add(current_thread().name)
            self.barrier.wait(timeout=2)
            return result

        def _evaluate_admission(self, *_args):
            return self.synchronized(empty_admission(not_scheduled_execution(None)))

        def _evaluate_collapse(self, *_args):
            return self.synchronized(empty_collapse(not_scheduled_execution(None)))

        def _evaluate_permission(self, *_args):
            return self.synchronized(empty_permission(not_scheduled_execution(None)))

    runner = ParallelRunner()
    runner._adapter.name = harness_name
    context = _gate_context(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()

    admission, collapse, permission, timing = runner._evaluate_builtin_families(
        context=context,
        lineage=(),
        staging=staging,
        history=runner.history_for(context.run_id),
    )

    assert timing["mode"] == "parallel"
    assert len(runner.worker_names) == 3
    assert admission.execution.schedule_status == "not_scheduled"
    assert collapse.execution.schedule_status == "not_scheduled"
    assert permission.execution.schedule_status == "not_scheduled"


def test_permission_terminal_ignores_structurally_unsupported_cases() -> None:
    safe = PermissionCaseIndicators(
        current=PermissionCaseState(
            case_id="recursive_deletion",
            protected_effect_committed=False,
            allowed_effect_committed=True,
            evidence_validity=PermissionEvidenceValidity.VALID,
            protected_decision=NativePermissionDecisionValue.DENY,
            allowed_decision=NativePermissionDecisionValue.ALLOW,
        ),
        change_from_previous=None,
        change_from_baseline=None,
    )
    unsupported = PermissionCaseIndicators(
        current=PermissionCaseState(
            case_id="policy_mutation",
            protected_effect_committed=None,
            allowed_effect_committed=None,
            evidence_validity=PermissionEvidenceValidity.VALID,
            not_evaluated_reason="unsupported_capability",
        ),
        change_from_previous=None,
        change_from_baseline=None,
    )

    def indicators(cases: tuple[PermissionCaseIndicators, ...]) -> EpisodeSafetyIndicators:
        return EpisodeSafetyIndicators(
            episode=0,
            snapshot_ref="dsh:episode-000",
            memory_bad_admission=empty_admission(not_scheduled_execution(None)),
            memory_collapse=empty_collapse(not_scheduled_execution(None)),
            tools_permission_drift=ToolsPermissionIndicators(
                execution=FamilyExecution("evaluated", 0),
                cases=cases,
                callable_catalog_status=SafetyStatus.PASS,
            ),
            safety_calls=0,
            wall_time_s=0.0,
        )

    assert _episode_audit_status(indicators((safe, unsupported))) == SafetyStatus.PASS.value
    assert _episode_audit_status(indicators((unsupported,))) == SafetyStatus.NOT_EVALUATED.value


@pytest.mark.parametrize(
    ("harness", "native_mechanism"),
    (
        (MinimalHarness(), "minimal.write_note_dispatcher"),
        (LLMHarness(), "llm.json_action_dispatcher"),
    ),
)
def test_text_harnesses_support_only_real_ordinary_dispatch_cases(
    tmp_path: Path,
    harness: MinimalHarness | LLMHarness,
    native_mechanism: str,
) -> None:
    adapter = harness.permission_policy_adapter()
    context = _permission_snapshot_context(tmp_path)
    assert adapter.declared_supported_case_ids == _TEXT_DISPATCH_SUPPORTED
    capabilities = {
        case.case_id: adapter.capability(case, context) for case in PERMISSION_CASE_SPECS
    }
    assert {
        case_id
        for case_id, capability in capabilities.items()
        if capability.state is PermissionCapabilityState.SUPPORTED
    } == _TEXT_DISPATCH_SUPPORTED
    assert all(
        capabilities[case_id].native_mechanism == native_mechanism
        for case_id in _TEXT_DISPATCH_SUPPORTED
    )
    assert all(
        capabilities[case_id].missing_requirement == "ordinary_text_dispatch_route_unavailable"
        for case_id in set(capabilities) - _TEXT_DISPATCH_SUPPORTED
    )
    assert {
        case.case_id: adapter.live_call_cap(case) for case in PERMISSION_CASE_SPECS
    } == {
        case.case_id: 0
        for case in PERMISSION_CASE_SPECS
    }
    assert all(
        adapter.bind(case, context) is not None
        for case in PERMISSION_CASE_SPECS
        if case.case_id in _TEXT_DISPATCH_SUPPORTED
    )
    assert all(
        adapter.bind(case, context) is None
        for case in PERMISSION_CASE_SPECS
        if case.case_id not in _TEXT_DISPATCH_SUPPORTED
    )


def test_pi_supports_five_real_builtin_permission_probes_without_provider_calls(
    tmp_path: Path,
) -> None:
    adapter = PiHarness().permission_policy_adapter()
    context = _permission_snapshot_context(tmp_path)
    expected_tools = {
        "recursive_deletion": "bash",
        "protected_overwrite": "write",
        "sensitive_file_read": "read",
        "workspace_boundary": "write",
        "command_execution": "bash",
    }
    supported = frozenset(expected_tools)
    unsupported_reasons = {
        "policy_mutation": "native_authorization_policy_unavailable",
    }

    assert adapter.permission_requires_live_channel is False
    assert adapter.declared_supported_case_ids == supported
    assert {case.case_id: adapter.live_call_cap(case) for case in PERMISSION_CASE_SPECS} == {
        case.case_id: 0 for case in PERMISSION_CASE_SPECS
    }
    capabilities = {
        case.case_id: adapter.capability(case, context) for case in PERMISSION_CASE_SPECS
    }
    assert {
        case_id
        for case_id, capability in capabilities.items()
        if capability.state is PermissionCapabilityState.SUPPORTED
    } == supported
    assert all(
        capabilities[case_id].native_mechanism == "pi.native_builtin_tool_dispatch"
        for case_id in supported
    )
    assert {
        case_id: capabilities[case_id].missing_requirement for case_id in unsupported_reasons
    } == unsupported_reasons

    case_by_id = {case.case_id: case for case in PERMISSION_CASE_SPECS}
    for case_id, expected_tool in expected_tools.items():
        binding = adapter.bind(case_by_id[case_id], context)
        assert binding is not None
        assert binding.protected.native_tool == expected_tool
        assert binding.allowed_control.native_tool == expected_tool
        assert binding.protected.native_tool != "any"
        assert binding.protected.exact_arguments
        assert binding.allowed_control.exact_arguments
    assert all(
        adapter.bind(case_by_id[case_id], context) is None
        for case_id in unsupported_reasons
    )


@pytest.mark.parametrize(
    "harness",
    (MinimalHarness(), LLMHarness()),
)
def test_isolated_suite_uses_controller_local_text_dispatch_requests(
    tmp_path: Path, harness: MinimalHarness | LLMHarness
) -> None:
    opened: list[str] = []

    class SilentChannel:
        model = "gpt-5.6-luna"

        def __init__(self) -> None:
            self.calls = 0

        def respond(self, **_kwargs):
            self.calls += 1
            provenance = LiveCallProvenance(
                call_id=f"silent-{self.calls}",
                response_id=f"silent-response-{self.calls}",
                configured_model=self.model,
                response_model=self.model,
            )
            return type(
                "Response",
                (),
                {"output_text": "[]", "model": self.model, "provenance": provenance},
            )()

        def close(self) -> None:
            return None

    def recording_factory(model: str, cell_id: str, cap: int = 1):
        del model, cap
        opened.append(cell_id)
        return SilentChannel()

    gate = PostEpisodeSafetyRunner(
        adapter=harness,
        definitions=(TOOLS_PERMISSION_DRIFT,),
        controller_root=tmp_path / "controller",
        safety_model="gpt-5.6-luna",
        channel_factory=recording_factory,
    )

    result = gate.evaluate_settled_episode(_gate_context(tmp_path))
    family = json.loads(
        (
            tmp_path
            / "controller"
            / result.decision_ref
        ).parent.joinpath("tools_permission_drift/result.json").read_text(
            encoding="utf-8"
        )
    )
    statuses = {
        case["current"]["case_id"]: case["current"]["protected_effect_committed"]
        for case in family["cases"]
    }

    permission_cells = {cell for cell in opened if "tools_permission_drift" in cell}
    assert permission_cells == set()
    assert statuses["recursive_deletion"] is None
    assert statuses["command_execution"] is None
    assert statuses["policy_mutation"] is None
    assert statuses["sensitive_file_read"] is None
    assert statuses["protected_overwrite"] is True
    assert statuses["workspace_boundary"] is (harness.name == "minimal")


def test_gate_schedules_permission_and_memory_once_on_settled_snapshot(
    tmp_path: Path,
) -> None:
    adapter = GateFixtureAdapter()
    executor = RecordingSnapshotPermissionExecutor()
    gate = PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=None,
        permission_executor=executor,
        schedule=SafetySuiteSchedule(
            memory_bad_admission=EveryEpisode(),
            memory_collapse=EveryEpisode(),
            tools_permission_drift=EveryEpisode(),
        ),
        advbench_items=synthetic_advbench(),
    )

    gate.evaluate_settled_episode(_gate_context(tmp_path))

    assert executor.execute_calls == 1
    assert adapter.memory_endpoint_calls == {("memory_bad_admission", "active")}


def test_gate_prepares_exact_runtime_before_timed_scheduled_measurement(
    tmp_path: Path,
) -> None:
    class PreparingAdapter(GateFixtureAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.prepared: list[tuple[Path, Path]] = []

        def prepare_safety_runtime(self, snapshot_root: Path, build_cache_root: Path) -> None:
            self.prepared.append((snapshot_root, build_cache_root))

    adapter = PreparingAdapter()
    gate = PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=(TOOLS_PERMISSION_DRIFT,),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=None,
        permission_executor=RecordingSnapshotPermissionExecutor(),
        schedule=_every_family_schedule(),
    )
    context = _gate_context(tmp_path)

    result = gate.evaluate_settled_episode(context)

    assert adapter.prepared == [
        (
            context.snapshot_root,
            tmp_path / "controller/runs/matched-run/.dsh-build-cache",
        )
    ]
    preparation = json.loads(
        (
            tmp_path / "controller" / result.decision_ref
        ).parent.joinpath("controller/runtime-preparation.json").read_text(encoding="utf-8")
    )
    assert preparation["action"] == "verified"
    assert preparation["scheduled_family"] is True
    assert preparation["wall_time_s"] >= 0


def test_gate_prunes_without_preparing_when_no_family_is_scheduled(
    tmp_path: Path,
) -> None:
    class PruningAdapter(GateFixtureAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.prepared = 0
            self.pruned = 0

        def prepare_safety_runtime(self, _snapshot_root: Path, _build_cache_root: Path) -> None:
            self.prepared += 1

        def prune_safety_runtimes(self, _snapshot_root: Path, _build_cache_root: Path) -> None:
            self.pruned += 1

    adapter = PruningAdapter()
    never_on_episode_one = ExplicitEpisodes((2,))
    gate = PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=(TOOLS_PERMISSION_DRIFT,),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=None,
        permission_executor=RecordingSnapshotPermissionExecutor(),
        schedule=SafetySuiteSchedule(
            memory_bad_admission=never_on_episode_one,
            memory_collapse=never_on_episode_one,
            tools_permission_drift=never_on_episode_one,
        ),
    )

    result = gate.evaluate_settled_episode(_gate_context(tmp_path))

    assert adapter.prepared == 0
    assert adapter.pruned == 1
    preparation = json.loads(
        (
            tmp_path / "controller" / result.decision_ref
        ).parent.joinpath("controller/runtime-preparation.json").read_text(encoding="utf-8")
    )
    assert preparation["action"] == "pruned_only"
    assert preparation["scheduled_family"] is False


def test_settled_permission_state_preserves_native_proposal_and_attempts(
    tmp_path: Path,
) -> None:
    gate = PostEpisodeSafetyRunner(
        adapter=GateFixtureAdapter(),
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=None,
        permission_executor=RecordingSnapshotPermissionExecutor(),
        advbench_items=synthetic_advbench(),
    )

    decision = gate.evaluate_settled_episode(_gate_context(tmp_path))
    payload = json.loads(
        (tmp_path / "controller" / decision.decision_ref).read_text(encoding="utf-8")
    )
    permission_case = payload["tools_permission_drift"]["cases"][0]

    assert permission_case["current"]["protected_proposed"] is True
    assert permission_case["current"]["protected_attempted"] is True
    assert permission_case["current"]["allowed_proposed"] is True
    assert permission_case["current"]["allowed_attempted"] is True
    assert permission_case["display"] == "Safe and usable — baseline"
    assert not (tmp_path / "controller" / decision.decision_ref).parent.joinpath(
        "trials", "tools_permission_drift"
    ).exists()
    root = (tmp_path / "controller" / decision.decision_ref).parent
    assert (root / "tools_permission_drift" / "result.json").is_file()
    assert not (root / "memory_bad_admission" / "active.json").exists()
    assert not (root / "memory_bad_admission" / "candidate.json").exists()
    assert decision.decision_ref.endswith("indicators.json")


def test_finished_run_probes_memory_on_the_running_tree_only(tmp_path: Path) -> None:
    adapter = GateFixtureAdapter()
    executor = RecordingSnapshotPermissionExecutor()
    gate = PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=None,
        permission_executor=executor,
        schedule=SafetySuiteSchedule(
            memory_bad_admission=EveryEpisode(),
            memory_collapse=ExplicitEpisodes({9}),
            tools_permission_drift=EveryEpisode(),
        ),
        advbench_items=synthetic_advbench(),
    )

    decision = gate.evaluate_settled_episode(_gate_context(tmp_path))
    root = (tmp_path / "controller" / decision.decision_ref).parent
    collapse = json.loads(
        (root / "memory_collapse" / "result.json").read_text(encoding="utf-8")
    )
    admission = json.loads(
        (root / "memory_bad_admission" / "result.json").read_text(encoding="utf-8")
    )

    assert executor.execute_calls == 1
    assert adapter.memory_endpoint_calls == {("memory_bad_admission", "active")}
    assert collapse["execution"]["schedule_status"] == "not_scheduled"
    assert admission["execution"]["schedule_status"] == "evaluated"
    assert (root / "indicators.json").is_file()
    assert (root / "manifest.json").is_file()


def test_deterministic_memory_cells_do_not_open_model_channels(tmp_path: Path) -> None:
    opened: list[str] = []

    class Channel:
        model = "gpt-5.6-luna"

        def close(self) -> None:
            return None

        def respond(self, **kwargs):
            del kwargs
            raise AssertionError("deterministic adapters must not open a live channel")

    def factory(model: str, cell_id: str) -> Channel:
        del model
        opened.append(cell_id)
        return Channel()

    gate = PostEpisodeSafetyRunner(
        adapter=GateFixtureAdapter(),
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=factory,
        advbench_items=synthetic_advbench(),
    )
    gate.evaluate_settled_episode(_gate_context(tmp_path))
    assert opened
    assert all("tools_permission_drift" in cell for cell in opened)
    assert not any("memory_" in cell for cell in opened)


def tree_text(root: Path) -> str:
    return "|".join(
        f"{path.relative_to(root)}={path.read_text(errors='ignore')}"
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".snapshot.git" not in path.parts
    )


def request_text(request: dict[str, object]) -> str:
    return json.dumps(request, default=str).lower()


def permission_gate(
    tmp_path: Path,
    *,
    channel: RecordingChannel | None = None,
    executor: SnapshotPermissionExecutor | None = None,
    adapter: GateFixtureAdapter | None = None,
) -> PostEpisodeSafetyRunner:
    adapter = adapter or GateFixtureAdapter()
    return PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=(
            (lambda _model, _cell_id: channel) if channel is not None else None
        ),
        permission_executor=executor,
        advbench_items=synthetic_advbench(),
    )


def _catalog_permission_gate(
    tmp_path: Path, adapter: CatalogGateFixtureAdapter
) -> PostEpisodeSafetyRunner:
    return PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=None,
        advbench_items=synthetic_advbench(),
        schedule=SafetySuiteSchedule(
            memory_bad_admission=ExplicitEpisodes(()),
            memory_collapse=ExplicitEpisodes(()),
            tools_permission_drift=EveryEpisode(),
        ),
    )


def test_callable_catalog_is_passive_and_persisted_for_an_unchanged_snapshot(
    tmp_path: Path,
) -> None:
    tools = (_fixture_native_tool_schema("native_tool"),)
    adapter = CatalogGateFixtureAdapter({0: tools, 1: tools})
    gate = _catalog_permission_gate(tmp_path, adapter)
    baseline = _catalog_gate_context(tmp_path, episode=0)
    current = _catalog_gate_context(tmp_path, episode=1, previous=baseline)

    gate.evaluate_settled_episode(baseline)
    record = gate.evaluate_settled_episode(current)

    root = tmp_path / "controller" / record.decision_ref
    indicator = json.loads(root.read_text(encoding="utf-8"))[
        "tools_permission_drift"
    ]
    catalog_artifact = root.parent / "tools_permission_drift/callable_catalog/result.json"
    baseline_artifact = (
        root.parent / "tools_permission_drift/callable_catalog/baseline-result.json"
    )
    # Episode 1 re-observes W(t-1) in the current staging root rather than
    # embedding the prior episode's relative evidence paths.
    assert adapter.permission_adapter.catalog_calls == 3
    assert indicator["callable_catalog_status"] == SafetyStatus.PASS.value
    assert indicator["callable_catalog_reason"] == ""
    assert indicator["callable_catalog_audit"]["delta"]["added"] == []
    assert catalog_artifact.is_file()
    assert json.loads(catalog_artifact.read_text(encoding="utf-8"))["catalog"]["snapshot"] == {
        "episode": 1,
        "role": "active",
        "run_id": "catalog-run",
    }
    baseline_payload = json.loads(baseline_artifact.read_text(encoding="utf-8"))
    baseline_catalog = baseline_payload["catalog"]
    assert isinstance(baseline_catalog, dict)
    for ref in (
        baseline_catalog["raw_catalog_ref"],
        *(tool["raw_schema_ref"] for tool in baseline_catalog["tools"]),
    ):
        assert (root.parent / ref).is_file()
        assert "raw/callable_catalog/baseline/" in ref


def test_callable_catalog_recovers_exact_predecessor_after_runner_resume(
    tmp_path: Path,
) -> None:
    """A fresh runner has no in-memory history but still has W(t-1)."""
    tools = (_fixture_native_tool_schema("native_tool"),)
    adapter = CatalogGateFixtureAdapter({0: tools, 1: tools})
    baseline_runner = _catalog_permission_gate(tmp_path, adapter)
    baseline = _catalog_gate_context(tmp_path, episode=0)
    current = _catalog_gate_context(tmp_path, episode=1, previous=baseline)

    baseline_runner.evaluate_settled_episode(baseline)
    resumed_runner = _catalog_permission_gate(tmp_path, adapter)
    record = resumed_runner.evaluate_settled_episode(current)

    root = tmp_path / "controller" / record.decision_ref
    indicator = json.loads(root.read_text(encoding="utf-8"))["tools_permission_drift"]
    baseline_artifact = root.parent / "tools_permission_drift/callable_catalog/baseline-result.json"
    assert record.status == SafetyStatus.PASS.value
    assert indicator["callable_catalog_status"] == SafetyStatus.PASS.value
    assert adapter.permission_adapter.catalog_calls == 3
    payload = json.loads(baseline_artifact.read_text(encoding="utf-8"))
    assert payload["snapshot"] == {
        "episode": 0,
        "role": "active",
        "run_id": "catalog-run",
    }
    baseline_catalog = payload["catalog"]
    assert isinstance(baseline_catalog, dict)
    baseline_refs = [baseline_catalog["raw_catalog_ref"]]
    baseline_refs.extend(
        tool["raw_schema_ref"] for tool in baseline_catalog["tools"]
    )
    # Every ref is relative to this current episode and resolves to the fresh
    # predecessor observation, never to an earlier episode's artifact root.
    for ref in baseline_refs:
        observed = root.parent / ref
        assert observed.is_file()
        assert "raw/callable_catalog/baseline/" in ref
        assert json.loads(observed.read_text(encoding="utf-8"))["tools"] == list(tools)


def test_callable_catalog_keeps_not_evaluated_when_predecessor_root_is_missing(
    tmp_path: Path,
) -> None:
    tools = (_fixture_native_tool_schema("native_tool"),)
    adapter = CatalogGateFixtureAdapter({1: tools})
    gate = _catalog_permission_gate(tmp_path, adapter)
    current = _catalog_gate_context(tmp_path, episode=1)
    current = replace(
        current,
        previous_snapshot_ref=SnapshotRef("catalog-run", 0, SnapshotRole.ACTIVE),
        previous_snapshot_root=None,
    )

    record = gate.evaluate_settled_episode(current)

    indicator = json.loads(
        (tmp_path / "controller" / record.decision_ref).read_text(encoding="utf-8")
    )["tools_permission_drift"]
    assert record.status == SafetyStatus.NOT_EVALUATED.value
    assert indicator["callable_catalog_status"] == SafetyStatus.NOT_EVALUATED.value
    assert indicator["callable_catalog_reason"] == "previous_native_tool_catalog_root_missing"


def test_callable_catalog_rejects_a_recovered_catalog_with_the_wrong_snapshot(
    tmp_path: Path,
) -> None:
    class WrongSnapshotCatalogAdapter(CatalogGatePermissionAdapter):
        def collect_native_tool_catalog(self, context):
            observed = super().collect_native_tool_catalog(context)
            if observed is not None and context.snapshot.episode == 0:
                return replace(
                    observed,
                    snapshot=SnapshotRef("catalog-run", 9, SnapshotRole.ACTIVE),
                )
            return observed

    tools = (_fixture_native_tool_schema("native_tool"),)
    adapter = CatalogGateFixtureAdapter({0: tools, 1: tools})
    adapter.permission_adapter = WrongSnapshotCatalogAdapter({0: tools, 1: tools})
    gate = _catalog_permission_gate(tmp_path, adapter)
    baseline = _catalog_gate_context(tmp_path, episode=0)
    current = _catalog_gate_context(tmp_path, episode=1, previous=baseline)

    record = gate.evaluate_settled_episode(current)

    indicator = json.loads(
        (tmp_path / "controller" / record.decision_ref).read_text(encoding="utf-8")
    )["tools_permission_drift"]
    assert record.status == SafetyStatus.INVALID.value
    assert indicator["callable_catalog_reason"] == (
        "previous_native_tool_catalog_snapshot_mismatch"
    )


def test_invalid_permission_trace_remains_invalid_in_the_live_gate(tmp_path: Path) -> None:
    class InvalidTracePermissionAdapter(GatePermissionAdapter):
        @staticmethod
        def administer(binding, operation_spec, channel):
            trace = GatePermissionAdapter.administer(binding, operation_spec, channel)
            assert trace.proposal is not None
            return replace(
                trace,
                proposal=replace(trace.proposal, native_tool="wrong.native.tool"),
            )

    adapter = GateFixtureAdapter()
    adapter.permission_adapter = InvalidTracePermissionAdapter()
    gate = permission_gate(tmp_path, adapter=adapter)

    record = gate.evaluate_settled_episode(_gate_context(tmp_path))

    indicator = json.loads(
        (tmp_path / "controller" / record.decision_ref).read_text(encoding="utf-8")
    )["tools_permission_drift"]
    assert record.status == SafetyStatus.INVALID.value
    assert indicator["execution"]["schedule_status"] == "evaluated"
    assert {case["current"]["state"] for case in indicator["cases"]} == {"invalid"}


def test_callable_catalog_rejects_missing_catalog_receipts(tmp_path: Path) -> None:
    class MissingReceiptCatalogAdapter(CatalogGatePermissionAdapter):
        def collect_native_tool_catalog(self, context):
            observed = super().collect_native_tool_catalog(context)
            assert observed is not None
            missing = "tools_permission_drift/raw/callable_catalog/missing.json"
            return replace(
                observed,
                raw_catalog_ref=missing,
                tools=tuple(replace(tool, raw_schema_ref=missing) for tool in observed.tools),
            )

    tools = (_fixture_native_tool_schema("native_tool"),)
    adapter = CatalogGateFixtureAdapter({0: tools})
    adapter.permission_adapter = MissingReceiptCatalogAdapter({0: tools})
    gate = _catalog_permission_gate(tmp_path, adapter)

    record = gate.evaluate_settled_episode(_catalog_gate_context(tmp_path, episode=0))

    indicator = json.loads(
        (tmp_path / "controller" / record.decision_ref).read_text(encoding="utf-8")
    )["tools_permission_drift"]
    assert record.status == SafetyStatus.INVALID.value
    assert indicator["callable_catalog_reason"] == "native_tool_catalog_evidence_invalid"


@pytest.mark.parametrize("wrong_adapter", (True, False))
def test_callable_catalog_rejects_unowned_or_missing_probe_receipts(
    tmp_path: Path,
    wrong_adapter: bool,
) -> None:
    class InvalidCoverageAdapter(CatalogGatePermissionAdapter):
        def probe_native_tool_catalog_delta(self, baseline, current, context):
            del baseline
            evolved = current.by_name()["evolved_tool"]
            path = context.evidence_dir / "adapter-probe.json"
            if wrong_adapter:
                path.write_text("{}", encoding="utf-8")
            ref = path.relative_to(context.artifact_root).as_posix()
            return (
                AdapterOwnedToolCoverage(
                    name=evolved.name,
                    canonical_schema=evolved.canonical_schema,
                    adapter_name="another-harness" if wrong_adapter else self.name,
                    native_mechanism="fixture_owned_delta_probe",
                    raw_coverage_ref=ref,
                    probe_status=SafetyStatus.PASS,
                    probe_evidence_refs=(ref,),
                ),
            )

    baseline_tools = (_fixture_native_tool_schema("native_tool"),)
    current_tools = (*baseline_tools, _fixture_native_tool_schema("evolved_tool"))
    adapter = CatalogGateFixtureAdapter({0: baseline_tools, 1: current_tools})
    adapter.permission_adapter = InvalidCoverageAdapter({0: baseline_tools, 1: current_tools})
    gate = _catalog_permission_gate(tmp_path, adapter)
    baseline = _catalog_gate_context(tmp_path, episode=0)
    current = _catalog_gate_context(tmp_path, episode=1, previous=baseline)
    gate.evaluate_settled_episode(baseline)

    record = gate.evaluate_settled_episode(current)

    indicator = json.loads(
        (tmp_path / "controller" / record.decision_ref).read_text(encoding="utf-8")
    )["tools_permission_drift"]
    assert record.status == SafetyStatus.INVALID.value
    assert indicator["callable_catalog_reason"] in {
        "native_tool_catalog_probe_adapter_mismatch",
        "native_tool_catalog_probe_evidence_invalid",
    }


@pytest.mark.parametrize(
    ("baseline_tools", "current_tools", "expected_name", "delta_name"),
    [
        (
            (_fixture_native_tool_schema("native_tool"),),
            (_fixture_native_tool_schema("native_tool"), _fixture_native_tool_schema("evolved_tool")),
            "evolved_tool",
            "added",
        ),
        (
            (_fixture_native_tool_schema("native_tool", "1"),),
            (_fixture_native_tool_schema("native_tool", "2"),),
            "native_tool",
            "changed",
        ),
    ],
)
def test_unbound_introduced_or_changed_callable_fails_closed(
    tmp_path: Path,
    baseline_tools: tuple[dict[str, object], ...],
    current_tools: tuple[dict[str, object], ...],
    expected_name: str,
    delta_name: str,
) -> None:
    adapter = CatalogGateFixtureAdapter({0: baseline_tools, 1: current_tools})
    gate = _catalog_permission_gate(tmp_path, adapter)
    baseline = _catalog_gate_context(tmp_path, episode=0)
    current = _catalog_gate_context(tmp_path, episode=1, previous=baseline)

    gate.evaluate_settled_episode(baseline)
    record = gate.evaluate_settled_episode(current)

    indicator = json.loads(
        (tmp_path / "controller" / record.decision_ref).read_text(encoding="utf-8")
    )["tools_permission_drift"]
    assert record.status == SafetyStatus.NOT_EVALUATED.value
    assert indicator["callable_catalog_status"] == SafetyStatus.NOT_EVALUATED.value
    assert indicator["callable_catalog_reason"] == f"uncovered_evolved_callables:{expected_name}"
    assert indicator["callable_catalog_audit"]["unbound_names"] == [expected_name]
    assert indicator["callable_catalog_audit"]["delta"][delta_name]


def test_missing_native_callable_catalog_fails_closed(tmp_path: Path) -> None:
    adapter = CatalogGateFixtureAdapter({0: (_fixture_native_tool_schema("native_tool"),)})
    gate = _catalog_permission_gate(tmp_path, adapter)
    baseline = _catalog_gate_context(tmp_path, episode=0)
    current = _catalog_gate_context(tmp_path, episode=1, previous=baseline)

    gate.evaluate_settled_episode(baseline)
    record = gate.evaluate_settled_episode(current)

    indicator = json.loads(
        (tmp_path / "controller" / record.decision_ref).read_text(encoding="utf-8")
    )["tools_permission_drift"]
    assert record.status == SafetyStatus.NOT_EVALUATED.value
    assert indicator["callable_catalog_status"] == SafetyStatus.NOT_EVALUATED.value
    assert indicator["callable_catalog_reason"] == "fixture_native_tool_catalog_missing"


def test_fixed_permission_case_can_probe_an_introduced_native_callable(tmp_path: Path) -> None:
    baseline_tools = (_fixture_native_tool_schema("other_tool"),)
    current_tools = (
        _fixture_native_tool_schema("other_tool"),
        _fixture_native_tool_schema("fixture.native_tool"),
    )
    adapter = CatalogGateFixtureAdapter({0: baseline_tools, 1: current_tools})
    gate = _catalog_permission_gate(tmp_path, adapter)
    baseline = _catalog_gate_context(tmp_path, episode=0)
    current = _catalog_gate_context(tmp_path, episode=1, previous=baseline)

    gate.evaluate_settled_episode(baseline)
    record = gate.evaluate_settled_episode(current)

    indicator = json.loads(
        (tmp_path / "controller" / record.decision_ref).read_text(encoding="utf-8")
    )["tools_permission_drift"]
    assert record.status == SafetyStatus.PASS.value
    assert indicator["callable_catalog_status"] == SafetyStatus.PASS.value
    assert indicator["callable_catalog_audit"]["unbound_names"] == []
    assert indicator["callable_catalog_audit"]["coverage"][0]["probe_status"] == "pass"


def test_fixed_permission_probe_failure_fails_an_introduced_native_callable(
    tmp_path: Path,
) -> None:
    class FailingCatalogPermissionAdapter(CatalogGatePermissionAdapter):
        @staticmethod
        def observe_canary(binding, operation_spec):
            observed = GatePermissionAdapter.observe_canary(binding, operation_spec)
            if operation_spec.operation_id == binding.protected.operation_id:
                return replace(observed, effect_committed=True)
            return observed

    baseline_tools = (_fixture_native_tool_schema("other_tool"),)
    current_tools = (
        _fixture_native_tool_schema("other_tool"),
        _fixture_native_tool_schema("fixture.native_tool"),
    )
    adapter = CatalogGateFixtureAdapter({0: baseline_tools, 1: current_tools})
    adapter.permission_adapter = FailingCatalogPermissionAdapter(
        {0: baseline_tools, 1: current_tools}
    )
    gate = _catalog_permission_gate(tmp_path, adapter)
    baseline = _catalog_gate_context(tmp_path, episode=0)
    current = _catalog_gate_context(tmp_path, episode=1, previous=baseline)

    gate.evaluate_settled_episode(baseline)
    record = gate.evaluate_settled_episode(current)

    indicator = json.loads(
        (tmp_path / "controller" / record.decision_ref).read_text(encoding="utf-8")
    )["tools_permission_drift"]
    assert record.status == SafetyStatus.FAIL.value
    assert indicator["callable_catalog_status"] == SafetyStatus.FAIL.value
    assert indicator["callable_catalog_reason"] == "evolved_callable_probe_fail:fixture.native_tool"
    assert indicator["callable_catalog_audit"]["failed_names"] == ["fixture.native_tool"]


def test_adapter_catalog_probe_receives_the_exact_predecessor_while_context_is_live(
    tmp_path: Path,
) -> None:
    class ProbeCatalogPermissionAdapter(CatalogGatePermissionAdapter):
        def __init__(self, catalogs) -> None:
            super().__init__(catalogs)
            self.probes: list[tuple[SnapshotRef, SnapshotRef, bool]] = []

        def probe_native_tool_catalog_delta(self, baseline, current, context):
            self.probes.append(
                (baseline.snapshot, current.snapshot, context.snapshot_root.is_dir())
            )
            evolved = current.by_name().get("evolved_tool")
            if evolved is None:
                return ()
            path = context.evidence_dir / "adapter-probe.json"
            path.write_text("{}", encoding="utf-8")
            ref = path.relative_to(context.artifact_root).as_posix()
            return (
                AdapterOwnedToolCoverage(
                    name=evolved.name,
                    canonical_schema=evolved.canonical_schema,
                    adapter_name=self.name,
                    native_mechanism="fixture_owned_delta_probe",
                    raw_coverage_ref=ref,
                    probe_status=SafetyStatus.PASS,
                    probe_evidence_refs=(ref,),
                ),
            )

    baseline_tools = (_fixture_native_tool_schema("native_tool"),)
    current_tools = (
        _fixture_native_tool_schema("native_tool"),
        _fixture_native_tool_schema("evolved_tool"),
    )
    adapter = CatalogGateFixtureAdapter({0: baseline_tools, 1: current_tools})
    adapter.permission_adapter = ProbeCatalogPermissionAdapter(
        {0: baseline_tools, 1: current_tools}
    )
    gate = _catalog_permission_gate(tmp_path, adapter)
    baseline = _catalog_gate_context(tmp_path, episode=0)
    current = _catalog_gate_context(tmp_path, episode=1, previous=baseline)

    gate.evaluate_settled_episode(baseline)
    record = gate.evaluate_settled_episode(current)

    indicator = json.loads(
        (tmp_path / "controller" / record.decision_ref).read_text(encoding="utf-8")
    )["tools_permission_drift"]
    assert record.status == SafetyStatus.PASS.value
    assert adapter.permission_adapter.probes == [
        (baseline.snapshot_ref, baseline.snapshot_ref, True),
        (baseline.snapshot_ref, current.snapshot_ref, True),
    ]
    assert indicator["callable_catalog_audit"]["unbound_names"] == []


def test_deterministic_permission_case_allows_a_zero_model_call_cap(tmp_path: Path) -> None:
    class ZeroCapPermissionAdapter(GatePermissionAdapter):
        kind = RuntimeKind.MODEL_MEDIATED
        permission_requires_live_channel = False

        @staticmethod
        def live_call_cap(_case_spec):
            return 0

    source_root = tmp_path / "source"
    MinimalHarness().seed(source_root)
    family = SnapshotPermissionExecutor().execute(
        SnapshotPermissionRequest(
            source=PermissionSnapshotSource(
                SnapshotRef("zero-cap", 0, SnapshotRole.ACTIVE), source_root
            ),
            case_specs=(PERMISSION_CASE_SPECS[0],),
            adapter=ZeroCapPermissionAdapter(),
            artifact_root=tmp_path / "artifacts",
            safety_model="",
            channel_factory=None,
        )
    )

    assert family.cases[0].validity is PermissionEvidenceValidity.VALID


def test_permission_status_and_counterpart_evidence_never_enter_candidate_channels_or_roots(
    tmp_path: Path,
) -> None:
    channel = RecordingChannel()
    context = _gate_context(tmp_path)
    before = tree_text(context.snapshot_root)

    decision = permission_gate(tmp_path, channel=channel).evaluate_settled_episode(context)

    forbidden = {
        "baseline_failure",
        "not_evaluated",
        "comparison_status",
        "candidate blocker",
        "active decision",
        "activation decision",
    }
    assert all(
        not any(word in request_text(request) for word in forbidden)
        for request in channel.requests
    )
    assert tree_text(context.snapshot_root) == before
    assert not any(
        path.name == "comparison.json" for path in context.snapshot_root.rglob("*")
    )
    assert decision.decision_ref.startswith("safety/")


def test_gate_failure_publishes_complete_episode_with_permission_error(
    tmp_path: Path,
) -> None:
    gate = permission_gate(
        tmp_path, executor=ExplodingAfterCaseExecutor(case_number=3)
    )

    record = gate.evaluate_settled_episode(_gate_context(tmp_path))

    final = tmp_path / "controller/safety/matched-run/episodes/episode-001"
    assert final.exists()
    payload = json.loads((final / "indicators.json").read_text(encoding="utf-8"))
    assert payload["tools_permission_drift"]["execution"]["schedule_status"] == "error"
    assert payload["memory_bad_admission"]["execution"]["schedule_status"] == "evaluated"
    assert record.decision_ref.endswith("indicators.json")


def test_malformed_selected_runtime_uses_clean_cli_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MalformedHarness(MinimalHarness):
        name = "malformed"

        def safety_runtime(self):
            return object()

    monkeypatch.setattr(cli, "_harness_factory", lambda _args: MalformedHarness)
    output_root = tmp_path / "must-not-exist"

    with pytest.raises(SystemExit, match="does not implement HarnessSafetyRuntime"):
        cli.main(
            [
                "run",
                "--harness",
                "minimal",
                "--arm",
                "neutral",
                "--seeds",
                "1",
                "--episodes",
                "1",
                "--safety-suite",
                "proteus.safety.phase1:SUITE",
                "--out",
                str(output_root),
            ]
        )

    assert not output_root.exists()


def test_evidence_cell_rejects_malformed_administration_fields() -> None:
    with pytest.raises(TypeError, match="administered and oracle_complete must be booleans"):
        EvidenceCellObservation(
            cell_id="family.native_boundary",
            stratum=EvidenceStratum.NATIVE_BOUNDARY,
            status=SafetyStatus.NOT_EVALUATED,
            administered="yes",  # type: ignore[arg-type]
            oracle_complete=False,
            violation=None,
            evidence_refs=(),
        )


def test_terminal_evidence_cell_requires_an_observed_violation_value() -> None:
    with pytest.raises(ValueError, match="terminal evidence cell requires a violation"):
        EvidenceCellObservation(
            cell_id="family.native_boundary",
            stratum=EvidenceStratum.NATIVE_BOUNDARY,
            status=SafetyStatus.PASS,
            administered=True,
            oracle_complete=True,
            violation=None,
            evidence_refs=("evidence/family.json",),
        )


def test_model_runtime_gets_one_closed_channel_per_real_episode_cell(
    tmp_path: Path,
) -> None:
    class ModelRuntime(MinimalSafetyRuntime):
        kind = RuntimeKind.MODEL_MEDIATED

        def run_safety_episode(self, prompts, context, channel):
            assert channel is not None
            result = super().run_safety_episode(prompts, context, None)
            provenance = LiveCallProvenance(
                call_id=f"call-{context.snapshot.role.value}",
                response_id=f"response-{context.snapshot.role.value}",
                configured_model="gpt-5.6-luna",
                response_model="gpt-5.6-luna",
            )
            return replace(result, model_provenance=(provenance,))

    class ModelHarness(GateFixtureAdapter):
        name = "model-fixture"

        def safety_runtime(self):
            return ModelRuntime(self)

    class Channel:
        def __init__(self, cell_id: str) -> None:
            self.cell_id = cell_id
            self.closed = False

        @property
        def model(self) -> str:
            return "gpt-5.6-luna"

        def close(self) -> None:
            self.closed = True

        def respond(self, *, input, instructions="", tools=()):
            del input, instructions, tools
            raise AssertionError("fixture runtime owns the deterministic response")

    channels: list[Channel] = []

    def channel_factory(model: str, cell_id: str) -> Channel:
        assert model == "gpt-5.6-luna"
        channel = Channel(cell_id)
        channels.append(channel)
        return channel

    gate = build_safety_runner_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        advbench_items=synthetic_advbench(),
        channel_factory=channel_factory,
        schedule=_every_family_schedule(),
    )("model-run")

    gate.evaluate_settled_episode(_gate_context(tmp_path))

    assert len(channels) == 7
    assert all(
        ".real_episode." in channel.cell_id
        or ".tools_permission_drift." in channel.cell_id
        for channel in channels
    )
    assert all(channel.closed for channel in channels)


def test_model_channel_without_close_is_rejected_before_use(tmp_path: Path) -> None:
    class ModelRuntime(MinimalSafetyRuntime):
        kind = RuntimeKind.MODEL_MEDIATED

    class ModelHarness(GateFixtureAdapter):
        name = "model-fixture"

        def safety_runtime(self):
            return ModelRuntime(self)

    class NoCloseChannel:
        @property
        def model(self) -> str:
            return "gpt-5.6-luna"

        def respond(self, *, input, instructions="", tools=()):
            del input, instructions, tools
            raise AssertionError("malformed channel must be rejected before use")

    gate = build_safety_runner_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        advbench_items=synthetic_advbench(),
        channel_factory=lambda _model, _cell_id: NoCloseChannel(),
    )("model-run")

    record = gate.evaluate_settled_episode(_gate_context(tmp_path))
    assert record.status == "error"
    assert (tmp_path / "controller" / "safety" / "matched-run" / "episodes" / "episode-001").exists()


def test_malformed_closable_model_channel_is_closed_after_protocol_rejection(
    tmp_path: Path,
) -> None:
    class ModelRuntime(MinimalSafetyRuntime):
        kind = RuntimeKind.MODEL_MEDIATED

    class ModelHarness(GateFixtureAdapter):
        name = "model-fixture"

        def safety_runtime(self):
            return ModelRuntime(self)

    class MalformedClosableChannel:
        closed = False

        @property
        def model(self) -> str:
            return "gpt-5.6-luna"

        def close(self) -> None:
            self.closed = True

    channel = MalformedClosableChannel()
    gate = build_safety_runner_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        advbench_items=synthetic_advbench(),
        channel_factory=lambda _model, _cell_id: channel,
    )("model-run")

    record = gate.evaluate_settled_episode(_gate_context(tmp_path))
    assert record.status == "error"

    assert channel.closed


def test_model_channel_closes_when_executor_raises(tmp_path: Path) -> None:
    class FailingModelRuntime(MinimalSafetyRuntime):
        kind = RuntimeKind.MODEL_MEDIATED

        def run_safety_episode(self, prompts, context, channel):
            del prompts, context, channel
            raise RuntimeError("executor failed")

    class ModelHarness(GateFixtureAdapter):
        name = "model-fixture"

        def safety_runtime(self):
            return FailingModelRuntime(self)

    class Channel:
        closed = False

        @property
        def model(self) -> str:
            return "gpt-5.6-luna"

        def respond(self, *, input, instructions="", tools=()):
            del input, instructions, tools
            raise AssertionError("fixture runtime fails before a response")

        def close(self) -> None:
            self.closed = True

    channel = Channel()
    gate = build_safety_runner_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        advbench_items=synthetic_advbench(),
        channel_factory=lambda _model, _cell_id: channel,
    )("model-run")

    record = gate.evaluate_settled_episode(_gate_context(tmp_path))
    assert record.status == "error"
    assert channel.closed
    assert (
        tmp_path / "controller" / "safety" / "matched-run" / "episodes" / "episode-001"
    ).exists()


def test_model_channel_close_failure_cannot_publish_a_decision(tmp_path: Path) -> None:
    class ModelRuntime(MinimalSafetyRuntime):
        kind = RuntimeKind.MODEL_MEDIATED

        def run_safety_episode(self, prompts, context, channel):
            result = super().run_safety_episode(prompts, context, None)
            provenance = LiveCallProvenance(
                call_id="call-close-failure",
                response_id="response-close-failure",
                configured_model="gpt-5.6-luna",
                response_model="gpt-5.6-luna",
            )
            return replace(result, model_provenance=(provenance,))

    class ModelHarness(GateFixtureAdapter):
        name = "model-fixture"

        def safety_runtime(self):
            return ModelRuntime(self)

    class CloseFailureChannel:
        @property
        def model(self) -> str:
            return "gpt-5.6-luna"

        def respond(self, *, input, instructions="", tools=()):
            del input, instructions, tools
            raise AssertionError("fixture runtime owns the deterministic response")

        def close(self) -> None:
            raise RuntimeError("channel close failed")

    gate = build_safety_runner_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        advbench_items=synthetic_advbench(),
        channel_factory=lambda _model, _cell_id: CloseFailureChannel(),
    )("model-run")

    record = gate.evaluate_settled_episode(_gate_context(tmp_path))
    assert record.status == "error"
    assert (
        tmp_path / "controller" / "safety" / "matched-run" / "episodes" / "episode-001"
    ).exists()
