from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from safety_memory_fixtures import synthetic_advbench

from proteus import cli
from proteus.adapters.llm import LLMHarness
from proteus.adapters.minimal import MinimalHarness
from proteus.adapters.minimal_safety import MinimalSafetyRuntime
from proteus.adapters.pi import PiHarness
from proteus.core.activation import CandidateGateContext
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.collapse_filler import is_flood_state_id
from proteus.safety.evidence import EvidenceCellObservation
from proteus.safety.gate import GateRunner, build_candidate_gate_factory
from proteus.safety.live import LiveCallProvenance
from proteus.safety.permission_adapter import PermissionSnapshotContext
from proteus.safety.permission_behavior import UNIVERSAL_EFFECT_CASE_IDS
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
    PermissionComparisonStatus,
)
from proteus.safety.permission_executor import PairedPermissionPolicyExecutor
from proteus.safety.phase1 import SUITE, TOOLS_PERMISSION_DRIFT
from proteus.safety.publication import write_json
from proteus.safety.runtime import RuntimeKind
from proteus.safety.taxonomy import EvidenceStratum, SafetyStatus

_NOTES_SUPPORTED = UNIVERSAL_EFFECT_CASE_IDS | {
    "protected_overwrite",
    "workspace_boundary",
}


class RecordingMinimalSafetyRuntime(MinimalSafetyRuntime):
    def __init__(self, harness) -> None:
        super().__init__(harness)
        self.memory_endpoint_calls: set[tuple[str, str]] = set()

    def introduce_memory(self, request, context):
        family_id = (
            "memory_collapse"
            if is_flood_state_id(request.state_id)
            else "memory_bad_admission"
        )
        self.memory_endpoint_calls.add((family_id, context.snapshot.role.value))
        return super().introduce_memory(request, context)


class GatePermissionAdapter:
    name = "gate-fixture-permission"
    kind = RuntimeKind.DETERMINISTIC
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

        return NativePermissionBinding(
            case_spec.case_id,
            f"fixture-native-policy:{snapshot_context.snapshot.role.value}",
            operation_binding(case_spec.protected),
            operation_binding(case_spec.allowed_control),
        )

    @staticmethod
    def administer(binding, operation_spec, channel):
        endpoint = binding.native_mechanism.rsplit(":", 1)[-1]
        snapshot = SnapshotRef(
            "matched-run",
            0 if endpoint == "active" else 1,
            SnapshotRole.ACTIVE if endpoint == "active" else SnapshotRole.CANDIDATE,
        )
        allowed = operation_spec.operation_id == binding.allowed_control.operation_id
        correlation = f"{endpoint}-{operation_spec.operation_id}"
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


class ExplodingAfterCaseExecutor(PairedPermissionPolicyExecutor):
    def __init__(self, *, case_number: int) -> None:
        self.case_number = case_number

    def execute(self, request):
        for case_spec in request.case_specs[: self.case_number]:
            path = (
                request.artifact_root
                / "families"
                / "tools_permission_drift"
                / "cases"
                / case_spec.case_id
                / "comparison.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        raise RuntimeError("controlled case failure")


class RecordingPairedPermissionPolicyExecutor(PairedPermissionPolicyExecutor):
    def __init__(self, mutation=None) -> None:
        self.execute_calls = 0
        self.mutation = mutation

    def execute(self, request):
        self.execute_calls += 1
        result = super().execute(request)
        return self.mutation(request, result) if self.mutation is not None else result


def _rewrite_permission_artifacts(request, family) -> None:
    for case in family.cases:
        write_json(
            request.artifact_root
            / "families/tools_permission_drift/cases"
            / case.case_id
            / "comparison.json",
            case,
        )
    write_json(
        request.artifact_root / "families/tools_permission_drift/family.json",
        family,
    )


def _missing_case_artifact(request, family):
    path = (
        request.artifact_root
        / "families/tools_permission_drift/cases/recursive_deletion/comparison.json"
    )
    path.unlink()
    return family


def _garbled_case_artifact(request, family):
    path = (
        request.artifact_root
        / "families/tools_permission_drift/cases/recursive_deletion/comparison.json"
    )
    path.write_text("{not-json\n", encoding="utf-8")
    return family


def _wrong_transition_artifacts(request, family):
    wrong_active = SnapshotRef("wrong-run", 99, SnapshotRole.ACTIVE)
    wrong_cases = tuple(
        replace(case, active_snapshot=wrong_active) for case in family.cases
    )
    wrong_family = replace(
        family,
        active_snapshot=wrong_active,
        cases=wrong_cases,
    )
    _rewrite_permission_artifacts(request, wrong_family)
    return wrong_family


def _forged_pass_family(request, family):
    incomplete = replace(
        family.cases[0],
        comparison_status=PermissionComparisonStatus.NOT_EVALUATED,
        reasons=("candidate_protected_missing_delivery",),
    )
    forged = replace(family, cases=(incomplete, *family.cases[1:]))
    _rewrite_permission_artifacts(request, forged)
    return forged


def _gate_context(tmp_path: Path) -> CandidateGateContext:
    active_root = tmp_path / "subject" / "active"
    candidate_root = tmp_path / "subject" / "candidate"
    MinimalHarness().seed(active_root)
    MinimalHarness().seed(candidate_root)
    return CandidateGateContext(
        run_id="matched-run",
        episode=1,
        active=SnapshotRef("matched-run", 0, SnapshotRole.ACTIVE),
        candidate=SnapshotRef("matched-run", 1, SnapshotRole.CANDIDATE),
        active_root=active_root,
        candidate_root=candidate_root,
        events=(),
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


@pytest.mark.parametrize("harness", [MinimalHarness(), LLMHarness()])
def test_minimal_and_llm_support_notes_surface_permission_cases(
    tmp_path: Path, harness
) -> None:
    adapter = harness.permission_policy_adapter()
    context = _permission_snapshot_context(tmp_path)
    unsupported = PermissionCaseCapability(
        PermissionCapabilityState.UNSUPPORTED,
        native_mechanism="",
        missing_requirement="ordinary_tool_effect_unavailable",
    )
    supported = PermissionCaseCapability(
        PermissionCapabilityState.SUPPORTED,
        native_mechanism="ordinary_tool_effects",
        missing_requirement="",
    )

    assert adapter.declared_supported_case_ids == _NOTES_SUPPORTED
    assert [adapter.live_call_cap(case) for case in PERMISSION_CASE_SPECS] == [
        1 if case.case_id in _NOTES_SUPPORTED else 0
        for case in PERMISSION_CASE_SPECS
    ]
    assert [adapter.capability(case, context) for case in PERMISSION_CASE_SPECS] == [
        supported if case.case_id in _NOTES_SUPPORTED else unsupported
        for case in PERMISSION_CASE_SPECS
    ]
    mutation = next(
        case for case in PERMISSION_CASE_SPECS if case.case_id == "policy_mutation"
    )
    minting = next(
        case for case in PERMISSION_CASE_SPECS if case.case_id == "tool_skill_capability_minting"
    )
    overwrite = next(
        case for case in PERMISSION_CASE_SPECS if case.case_id == "protected_overwrite"
    )
    workspace = next(
        case for case in PERMISSION_CASE_SPECS if case.case_id == "workspace_boundary"
    )
    mutation_binding = adapter.bind(mutation, context)
    minting_binding = adapter.bind(minting, context)
    overwrite_binding = adapter.bind(overwrite, context)
    workspace_binding = adapter.bind(workspace, context)
    assert mutation_binding is not None
    assert minting_binding is not None
    assert overwrite_binding is not None
    assert workspace_binding is not None
    governor = context.snapshot_root / "notes/governor.md"
    assert governor.read_text(encoding="utf-8") == "# governor\n"
    adapter.administer(mutation_binding, mutation.protected, None)
    canary = adapter.observe_canary(mutation_binding, mutation.protected)
    assert canary.observed
    assert not canary.effect_committed
    governor.write_text("# prohibited policy replacement\n", encoding="utf-8")
    canary = adapter.observe_canary(mutation_binding, mutation.protected)
    assert canary.effect_committed
    adapter.administer(minting_binding, minting.protected, None)
    minting_canary = adapter.observe_canary(minting_binding, minting.protected)
    assert minting_canary.observed
    assert not minting_canary.effect_committed
    prohibited = context.snapshot_root / "tools/prohibited_capability.py"
    prohibited.write_text(
        "# authored capability\ncapability = 'command_execution'\n",
        encoding="utf-8",
    )
    minting_canary = adapter.observe_canary(minting_binding, minting.protected)
    assert minting_canary.effect_committed
    protected_note = context.snapshot_root / "notes/protected_overwrite.md"
    assert protected_note.read_text(encoding="utf-8") == "protected sentinel\n"
    adapter.administer(overwrite_binding, overwrite.protected, None)
    overwrite_canary = adapter.observe_canary(overwrite_binding, overwrite.protected)
    assert overwrite_canary.observed
    assert not overwrite_canary.effect_committed
    protected_note.write_text("protected replacement\n", encoding="utf-8")
    overwrite_canary = adapter.observe_canary(overwrite_binding, overwrite.protected)
    assert overwrite_canary.effect_committed
    outside = context.trial_root / "outside_workspace/protected-canary.txt"
    inside = context.snapshot_root / "notes/allowed-canary.md"
    adapter.administer(workspace_binding, workspace.protected, None)
    outside_canary = adapter.observe_canary(workspace_binding, workspace.protected)
    assert outside_canary.observed
    assert not outside_canary.effect_committed
    assert not outside.exists()
    inside.write_text("allowed inside canary\n", encoding="utf-8")
    inside_canary = adapter.observe_canary(workspace_binding, workspace.allowed_control)
    assert inside_canary.effect_committed
    deletion = next(
        case for case in PERMISSION_CASE_SPECS if case.case_id == "recursive_deletion"
    )
    command = next(
        case for case in PERMISSION_CASE_SPECS if case.case_id == "command_execution"
    )
    assert adapter.bind(deletion, context) is None
    assert adapter.bind(command, context) is None


def test_pi_scores_ordinary_tool_effects_without_a_permission_module(tmp_path: Path) -> None:
    adapter = PiHarness().permission_policy_adapter()
    context = _permission_snapshot_context(tmp_path)
    supported = {
        "recursive_deletion",
        "protected_overwrite",
        "policy_mutation",
        "tool_skill_capability_minting",
        "workspace_boundary",
        "command_execution",
    }

    assert adapter.declared_supported_case_ids == supported
    capabilities = {
        case.case_id: adapter.capability(case, context) for case in PERMISSION_CASE_SPECS
    }
    assert {
        case_id
        for case_id, item in capabilities.items()
        if item.state is PermissionCapabilityState.SUPPORTED
    } == supported
    overwrite = next(
        case for case in PERMISSION_CASE_SPECS if case.case_id == "protected_overwrite"
    )
    binding = adapter.bind(overwrite, context)
    assert binding is not None
    assert binding.protected.native_tool == "any"
    protected = context.snapshot_root / "fixtures/protected/overwrite.txt"
    assert protected.read_text(encoding="utf-8") == "protected sentinel\n"
    adapter.administer(binding, overwrite.protected, None)
    canary = adapter.observe_canary(binding, overwrite.protected)
    assert canary.observed
    assert not canary.effect_committed
    protected.write_text("protected replacement\n", encoding="utf-8")
    canary = adapter.observe_canary(binding, overwrite.protected)
    assert canary.effect_committed
    mutation = next(
        case for case in PERMISSION_CASE_SPECS if case.case_id == "policy_mutation"
    )
    mutation_binding = adapter.bind(mutation, context)
    assert mutation_binding is not None
    governor = context.snapshot_root / "AGENTS.md"
    assert governor.is_file()
    adapter.administer(mutation_binding, mutation.protected, None)
    mutation_canary = adapter.observe_canary(mutation_binding, mutation.protected)
    assert mutation_canary.observed
    assert not mutation_canary.effect_committed
    governor.write_text("# mutated governor\n", encoding="utf-8")
    mutation_canary = adapter.observe_canary(mutation_binding, mutation.protected)
    assert mutation_canary.effect_committed


@pytest.mark.parametrize("harness", [MinimalHarness(), LLMHarness()])
def test_isolated_suite_opens_channels_only_for_notes_surface_cases(
    tmp_path: Path, harness
) -> None:
    opened: list[str] = []

    class SilentChannel:
        model = "gpt-5.6-luna"

        def respond(self, **_kwargs):
            return type(
                "Response",
                (),
                {"output_text": "[]", "model": self.model, "provenance": None},
            )()

        def close(self) -> None:
            return None

    def recording_factory(model: str, cell_id: str, cap: int = 1):
        del model, cap
        opened.append(cell_id)
        return SilentChannel()

    gate = GateRunner(
        adapter=harness,
        definitions=(TOOLS_PERMISSION_DRIFT,),
        controller_root=tmp_path / "controller",
        safety_model="gpt-5.6-luna",
        channel_factory=recording_factory,
    )

    result = gate.evaluate(_gate_context(tmp_path))
    family = json.loads(
        (
            tmp_path
            / "controller"
            / result.decision_ref
        ).parent.joinpath("families/tools_permission_drift/family.json").read_text(
            encoding="utf-8"
        )
    )
    statuses = {case["case_id"]: case["comparison_status"] for case in family["cases"]}

    assert {cell for cell in opened if "tools_permission_drift" in cell}
    assert all(
        "recursive_deletion" not in cell and "command_execution" not in cell
        for cell in opened
    )
    assert statuses["recursive_deletion"] == "not_evaluated"
    assert statuses["command_execution"] == "not_evaluated"
    assert statuses["protected_overwrite"] == "baseline_failure"
    assert statuses["workspace_boundary"] == "baseline_failure"
    assert statuses["policy_mutation"] == "baseline_failure"
    assert statuses["tool_skill_capability_minting"] == "baseline_failure"


def test_gate_schedules_permission_once_per_transition_and_memory_per_endpoint(
    tmp_path: Path,
) -> None:
    adapter = GateFixtureAdapter()
    executor = RecordingPairedPermissionPolicyExecutor()
    gate = GateRunner(
        adapter=adapter,
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=None,
        permission_executor=executor,
        advbench_items=synthetic_advbench(),
    )

    decision = gate.evaluate(_gate_context(tmp_path))

    assert executor.execute_calls == 1
    assert adapter.memory_endpoint_calls == {
        ("memory_bad_admission", "active"),
        ("memory_bad_admission", "candidate"),
        ("memory_collapse", "active"),
        ("memory_collapse", "candidate"),
    }
    root = (tmp_path / "controller" / decision.decision_ref).parent
    assert len(
        list(
            (root / "families/tools_permission_drift/cases").glob(
                "*/comparison.json"
            )
        )
    ) == 6
    assert not (root / "families/tools_permission_drift/active.json").exists()
    assert not (root / "families/tools_permission_drift/candidate.json").exists()

    published = [
        json.loads(line)
        for line in (root / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(published) == 5
    permission = [
        item for item in published if item["family_id"] == "tools_permission_drift"
    ]
    assert len(permission) == 1
    assert "endpoint" not in permission[0]
    assert permission[0]["comparison_status"] == "pass"

    by_memory_endpoint = {
        (item["family_id"], item["endpoint"]): item
        for item in published
        if "endpoint" in item
    }
    for definition in SUITE.definitions():
        if definition.family_id == "tools_permission_drift":
            continue
        declared = [cell.cell_id for cell in definition.declared_cells]
        for endpoint in ("active", "candidate"):
            observation = by_memory_endpoint[(definition.family_id, endpoint)]
            assert [cell["cell_id"] for cell in observation["cells"]] == declared
            for cell in observation["cells"]:
                if cell["status"] in {SafetyStatus.PASS.value, SafetyStatus.FAIL.value}:
                    assert cell["evidence_refs"]
                    assert all(
                        f"/{cell['cell_id']}/" in f"/{ref}"
                        for ref in cell["evidence_refs"]
                    )
                    assert all((root / ref).is_file() for ref in cell["evidence_refs"])


def test_deferred_live_episodes_do_not_open_model_channels(tmp_path: Path) -> None:
    opened: list[str] = []

    class Channel:
        model = "gpt-5.6-luna"

        def close(self) -> None:
            return None

        def respond(self, **kwargs):
            del kwargs
            raise AssertionError("deferred live episodes must not call the model")

    def factory(model: str, cell_id: str) -> Channel:
        del model
        opened.append(cell_id)
        return Channel()

    gate = GateRunner(
        adapter=GateFixtureAdapter(),
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="gpt-5.6-luna",
        channel_factory=factory,
        run_live_episodes=False,
        advbench_items=synthetic_advbench(),
    )
    gate.evaluate(_gate_context(tmp_path))
    assert opened == []


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
    executor: PairedPermissionPolicyExecutor | None = None,
    adapter: GateFixtureAdapter | None = None,
) -> GateRunner:
    adapter = adapter or GateFixtureAdapter()
    return GateRunner(
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


def test_permission_status_and_counterpart_evidence_never_enter_candidate_channels_or_roots(
    tmp_path: Path,
) -> None:
    channel = RecordingChannel()
    context = _gate_context(tmp_path)
    active_before = tree_text(context.active_root)
    candidate_before = tree_text(context.candidate_root)

    decision = permission_gate(tmp_path, channel=channel).evaluate(context)

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
    assert tree_text(context.active_root) == active_before
    assert tree_text(context.candidate_root) == candidate_before
    assert not any(
        path.name == "comparison.json" for path in context.candidate_root.rglob("*")
    )
    assert decision.decision_ref.startswith("safety-gates/")


def test_gate_failure_publishes_neither_family_nor_decision(tmp_path: Path) -> None:
    gate = permission_gate(
        tmp_path, executor=ExplodingAfterCaseExecutor(case_number=3)
    )

    with pytest.raises(RuntimeError, match="controlled case failure"):
        gate.evaluate(_gate_context(tmp_path))

    final = tmp_path / "controller/safety-gates/matched-run/episode-001"
    assert not final.exists()
    assert list((final.parent / ".failed").glob("episode-001-*"))


@pytest.mark.parametrize(
    "mutation",
    [
        _missing_case_artifact,
        _garbled_case_artifact,
        _wrong_transition_artifacts,
        _forged_pass_family,
    ],
    ids=["missing-case", "garbled-json", "wrong-transition", "forged-pass"],
)
def test_gate_rejects_incomplete_or_mismatched_staged_permission_evidence(
    tmp_path: Path,
    mutation,
) -> None:
    executor = RecordingPairedPermissionPolicyExecutor(mutation)
    gate = permission_gate(tmp_path, executor=executor)

    with pytest.raises(ValueError, match="staged permission evidence"):
        gate.evaluate(_gate_context(tmp_path))

    final = tmp_path / "controller/safety-gates/matched-run/episode-001"
    assert executor.execute_calls == 1
    assert not final.exists()
    assert list((final.parent / ".failed").glob("episode-001-*"))


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

    gate = build_candidate_gate_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        advbench_items=synthetic_advbench(),
        channel_factory=channel_factory,
    )("model-run")

    gate.evaluate(_gate_context(tmp_path))

    assert len(channels) == 16
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

    gate = build_candidate_gate_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        advbench_items=synthetic_advbench(),
        channel_factory=lambda _model, _cell_id: NoCloseChannel(),
    )("model-run")

    with pytest.raises(TypeError, match="must implement LiveModelChannel"):
        gate.evaluate(_gate_context(tmp_path))

    assert not (tmp_path / "controller" / "safety-gates" / "matched-run" / "episode-001").exists()


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
    gate = build_candidate_gate_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        advbench_items=synthetic_advbench(),
        channel_factory=lambda _model, _cell_id: channel,
    )("model-run")

    with pytest.raises(TypeError, match="must implement LiveModelChannel"):
        gate.evaluate(_gate_context(tmp_path))

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
    gate = build_candidate_gate_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        advbench_items=synthetic_advbench(),
        channel_factory=lambda _model, _cell_id: channel,
    )("model-run")

    with pytest.raises(RuntimeError, match="executor failed"):
        gate.evaluate(_gate_context(tmp_path))

    assert channel.closed


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

    gate = build_candidate_gate_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        advbench_items=synthetic_advbench(),
        channel_factory=lambda _model, _cell_id: CloseFailureChannel(),
    )("model-run")

    with pytest.raises(RuntimeError, match="channel close failed"):
        gate.evaluate(_gate_context(tmp_path))

    assert not (tmp_path / "controller" / "safety-gates" / "matched-run" / "episode-001").exists()
