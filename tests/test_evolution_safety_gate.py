from __future__ import annotations

import json
import time
from dataclasses import dataclass, fields
from pathlib import Path

import pytest

from proteus.core.activation import CandidateGateContext, CandidateGateResult
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import (
    InvariantObservation,
    ProbeEndpoint,
    ProbeObservation,
    ProbeStatuses,
    StageValue,
    UtilityObservation,
)
from proteus.safety.gate import GateRunner
from proteus.safety.indicators import (
    EvolutionSafetyProfile,
    IndicatorAssessment,
    IndicatorComponent,
    IndicatorDirection,
    MatchedProbeObservations,
)
from proteus.safety.live import LiveCallProvenance, LiveModelConfig
from proteus.safety.policy import SafetyGateStatus, evaluate_safety_policy
from proteus.safety.publication import AtomicCandidatePublication
from proteus.safety.taxonomy import (
    CaseScope,
    EvaluationArm,
    EvidenceStratum,
    FaultModel,
    HarnessModule,
    HarnessSafetyProfile,
    IndicatorRequirement,
    ModuleBinding,
    SafetyCaseFamilyDefinition,
    SafetyExposure,
    SafetyIndicator,
    SafetyInvariantDefinition,
    SafetyKind,
    SafetyStatus,
)


def _snapshots() -> tuple[SnapshotRef, SnapshotRef]:
    return (
        SnapshotRef("run-1", 0, SnapshotRole.ACTIVE),
        SnapshotRef("run-1", 1, SnapshotRole.CANDIDATE),
    )


def _family(
    indicator: SafetyIndicator = SafetyIndicator.INVARIANT_PRESERVATION,
    *,
    critical: bool = True,
    strata: tuple[EvidenceStratum, ...] = (EvidenceStratum.DETERMINISTIC_BOUNDARY,),
    utility_minimum: float = 0.0,
    arms: tuple[EvaluationArm, ...] = (
        EvaluationArm.MODEL_REFERENCE,
        EvaluationArm.FULL_HARNESS,
    ),
) -> SafetyCaseFamilyDefinition:
    return SafetyCaseFamilyDefinition(
        family_id="family-1",
        family_version="1",
        primary_module=HarnessModule.MEMORY,
        supporting_modules=(),
        scope=CaseScope.SINGLE_MODULE,
        safety_kind=SafetyKind.NON_ADVERSARIAL,
        scenario="controlled scenario",
        invariant=SafetyInvariantDefinition("family-1.invariant", "the invariant holds"),
        indicator_requirements=(IndicatorRequirement(indicator, critical, strata),),
        utility_minimum=utility_minimum,
        exposure_rule="the native path is directly observable",
        behavior_failure="a harmful behavior occurs",
        module_failure="the module violates the invariant",
        evaluation_arms=arms,
        intervention_expected_violation=(
            False if EvaluationArm.MODULE_INTERVENTION in arms else None
        ),
        fault_model=FaultModel("controlled source", "controlled condition"),
    )


def _component(
    component_id: str,
    *,
    status: SafetyStatus = SafetyStatus.PASS,
    direction: IndicatorDirection = IndicatorDirection.SAME,
    value: object = 0,
) -> IndicatorComponent:
    evaluated = 0 if status is SafetyStatus.NOT_EVALUATED else 1
    return IndicatorComponent(
        component_id=component_id,
        status=status,
        direction=direction,
        planned=1,
        eligible=1,
        evaluated=evaluated,
        value=value,
        evidence_refs=("evidence/oracle.json",),
    )


def _profile(
    indicator: SafetyIndicator,
    *components: IndicatorComponent,
    status: SafetyStatus | None = None,
) -> EvolutionSafetyProfile:
    active, candidate = _snapshots()
    assessment = IndicatorAssessment(
        indicator=indicator,
        status=status or components[0].status,
        direction=components[0].direction,
        components=tuple(components),
    )
    return EvolutionSafetyProfile(active, candidate, {"family-1": (assessment,)})


@pytest.mark.parametrize(
    ("indicator", "component", "status", "direction", "value", "code"),
    [
        (
            SafetyIndicator.INVARIANT_PRESERVATION,
            "conditional_regression",
            SafetyStatus.FAIL,
            IndicatorDirection.WORSE,
            1.0,
            "critical_invariant_regression",
        ),
        (
            SafetyIndicator.PERMISSION_BOUNDARY_INTEGRITY,
            "unauthorized_expansion_drift",
            SafetyStatus.FAIL,
            IndicatorDirection.WORSE,
            ("effect-1",),
            "unauthorized_expansion_drift",
        ),
        (
            SafetyIndicator.PERMISSION_BOUNDARY_INTEGRITY,
            "revocation_enforcement_failure",
            SafetyStatus.FAIL,
            IndicatorDirection.WORSE,
            ("effect-1",),
            "revocation_enforcement_failure",
        ),
        (
            SafetyIndicator.HARM_CONTAINMENT,
            "harm_committed",
            SafetyStatus.FAIL,
            IndicatorDirection.SAME,
            1,
            "critical_harm_commit",
        ),
        (
            SafetyIndicator.HARM_CONTAINMENT,
            "harm_committed",
            SafetyStatus.PASS,
            IndicatorDirection.WORSE,
            0,
            "worsening_harm_commit",
        ),
        (
            SafetyIndicator.UNSAFE_STATE_PROPAGATION,
            "active_lineage_persistence",
            SafetyStatus.FAIL,
            IndicatorDirection.WORSE,
            ("state:true",),
            "worsening_active_lineage_persistence",
        ),
    ],
)
def test_policy_blocks_each_observed_critical_rule(
    indicator,
    component,
    status,
    direction,
    value,
    code,
) -> None:
    family = _family(indicator)
    profile = _profile(
        indicator,
        _component(component, status=status, direction=direction, value=value),
        status=status,
    )

    decision = evaluate_safety_policy(profile, (family,), {})

    assert decision.allowed is False
    assert decision.status is SafetyGateStatus.FAIL
    assert code in {blocker.code for blocker in decision.blockers}


@pytest.mark.parametrize(
    ("source_status", "terminal"),
    [
        (SafetyStatus.NOT_EVALUATED, SafetyGateStatus.NOT_EVALUATED),
        (SafetyStatus.INVALID, SafetyGateStatus.INVALID),
        (SafetyStatus.ERROR, SafetyGateStatus.ERROR),
    ],
)
def test_policy_fails_closed_on_critical_terminal_status(source_status, terminal) -> None:
    family = _family()
    profile = _profile(
        SafetyIndicator.INVARIANT_PRESERVATION,
        _component("conditional_regression", status=source_status, value=None),
        status=source_status,
    )

    decision = evaluate_safety_policy(profile, (family,), {})

    assert decision.allowed is False
    assert decision.status is terminal
    assert decision.blockers[0].code == f"critical_{source_status.value}"


def test_policy_maps_critical_not_exposed_to_not_evaluated() -> None:
    family = _family()
    profile = _profile(
        SafetyIndicator.INVARIANT_PRESERVATION,
        _component(
            "conditional_regression",
            status=SafetyStatus.NOT_EVALUATED,
            direction=IndicatorDirection.UNKNOWN,
            value=None,
        ),
        status=SafetyStatus.NOT_EVALUATED,
    )
    active, candidate = _snapshots()
    pair = MatchedProbeObservations(
        ProbeObservation(
            snapshot=active,
            endpoint=ProbeEndpoint.ACTIVE,
            arm=EvaluationArm.FULL_HARNESS,
            stratum=EvidenceStratum.DETERMINISTIC_BOUNDARY,
            exposure=SafetyExposure.NOT_EXPOSED,
        ),
        ProbeObservation(
            snapshot=candidate,
            endpoint=ProbeEndpoint.CANDIDATE,
            arm=EvaluationArm.FULL_HARNESS,
            stratum=EvidenceStratum.DETERMINISTIC_BOUNDARY,
            exposure=SafetyExposure.NOT_EXPOSED,
        ),
    )

    decision = evaluate_safety_policy(profile, (family,), {"family-1": (pair,)})

    assert decision.status is SafetyGateStatus.NOT_EVALUATED
    assert "critical_not_exposed" in {blocker.code for blocker in decision.blockers}


def test_policy_blocks_matched_benign_utility_below_declared_minimum() -> None:
    family = _family(utility_minimum=0.75)
    profile = _profile(
        SafetyIndicator.INVARIANT_PRESERVATION,
        _component("conditional_regression"),
    )
    active, candidate = _snapshots()

    def observation(snapshot, endpoint, completed):
        return ProbeObservation(
            snapshot=snapshot,
            endpoint=endpoint,
            arm=EvaluationArm.FULL_HARNESS,
            stratum=EvidenceStratum.DETERMINISTIC_BOUNDARY,
            statuses=ProbeStatuses(utility=SafetyStatus.PASS),
            utilities=(
                UtilityObservation("benign-1", StageValue.TRUE, completed),
                UtilityObservation("benign-2", StageValue.TRUE, StageValue.TRUE),
            ),
            evidence_refs=(f"evidence/{endpoint.value}.json",),
        )

    pair = MatchedProbeObservations(
        observation(active, ProbeEndpoint.ACTIVE, StageValue.TRUE),
        observation(candidate, ProbeEndpoint.CANDIDATE, StageValue.FALSE),
    )

    decision = evaluate_safety_policy(profile, (family,), {"family-1": (pair,)})

    assert decision.status is SafetyGateStatus.FAIL
    assert decision.blockers[-1].code == "matched_benign_utility_below_minimum"


def test_noncritical_unknown_warns_and_persistent_baseline_failure_does_not_block() -> None:
    family = _family(critical=False)
    profile = _profile(
        SafetyIndicator.INVARIANT_PRESERVATION,
        _component(
            "persistently_unsafe",
            status=SafetyStatus.NOT_EVALUATED,
            direction=IndicatorDirection.UNKNOWN,
            value=None,
        ),
        status=SafetyStatus.NOT_EVALUATED,
    )

    decision = evaluate_safety_policy(profile, (family,), {})

    assert decision.allowed is True
    assert decision.status is SafetyGateStatus.PASS
    assert decision.blockers == ()
    assert decision.warnings == (
        "family-1:invariant_preservation:persistently_unsafe:not_evaluated",
    )


@dataclass(frozen=True)
class _Suite:
    families: tuple[SafetyCaseFamilyDefinition, ...]
    name: str = "test-suite"
    version: str = "1"

    def definitions(self):
        return self.families


class _Channel:
    model = "gpt-fixed"


class _Broker:
    def channel(self, _cell_id: str):
        return _Channel()


class _Executor:
    name = "recording-executor"

    def __init__(self, mode: str = "pass") -> None:
        self.mode = mode
        self.calls: list[
            tuple[ProbeEndpoint, EvaluationArm, EvidenceStratum, Path, Path, Path]
        ] = []

    def collect(self, definition, endpoint, arm, stratum, context, channel):
        self.calls.append(
            (
                endpoint,
                arm,
                stratum,
                context.snapshot_root,
                context.trial_root,
                context.evidence_dir,
            )
        )
        assert context.snapshot_root.is_dir()
        assert not (context.snapshot_root / "cell-marker.txt").exists()
        (context.snapshot_root / "cell-marker.txt").write_text("cell-local", encoding="utf-8")
        if self.mode == "exception":
            raise RuntimeError("sk-secret-must-not-be-published")
        if self.mode == "timeout":
            time.sleep(0.05)
        if self.mode == "wrong-type":
            return {"status": "pass"}
        if self.mode == "not-exposed":
            return ProbeObservation(
                snapshot=context.snapshot,
                endpoint=endpoint,
                arm=arm,
                stratum=stratum,
                exposure=SafetyExposure.NOT_EXPOSED,
            )

        context.evidence_dir.mkdir(parents=True, exist_ok=True)
        (context.evidence_dir / "oracle.json").write_text('{"direct":true}\n')
        ref = (
            Path("evidence")
            / definition.family_id
            / endpoint.value
            / arm.value
            / f"trial-{stratum.value}-0001"
            / "oracle.json"
        ).as_posix()
        provenance = ()
        if stratum is EvidenceStratum.FIXED_LIVE_BEHAVIOR:
            model = "wrong-model" if self.mode == "wrong-model" else "gpt-fixed"
            provenance = (LiveCallProvenance("call-1", "response-1", model, model),)
        invariants = () if self.mode == "missing-oracle" else (
            InvariantObservation(definition.invariant.invariant_id, StageValue.TRUE),
        )
        refs = ("evidence/missing.json",) if self.mode == "missing-ref" else (ref,)
        return ProbeObservation(
            snapshot=context.snapshot,
            endpoint=endpoint,
            arm=arm,
            stratum=stratum,
            statuses=ProbeStatuses(module=SafetyStatus.PASS),
            exposure=SafetyExposure.EXPOSED,
            invariants=invariants,
            model_provenance=provenance,
            evidence_refs=refs,
        )


class _Adapter:
    def __init__(self, executor: _Executor) -> None:
        self.executor = executor

    def harness_safety_profile(self):
        return HarnessSafetyProfile((ModuleBinding(HarnessModule.MEMORY, runtime_evidence=True),))

    def candidate_safety_executor(self):
        return self.executor


def _context(tmp_path: Path) -> CandidateGateContext:
    active, candidate = _snapshots()
    run_root = tmp_path / "subject-run"
    active_root = run_root / "active"
    candidate_root = run_root / "candidate"
    active_root.mkdir(parents=True)
    candidate_root.mkdir(parents=True)
    (active_root / "state.txt").write_text("active", encoding="utf-8")
    (candidate_root / "state.txt").write_text("candidate", encoding="utf-8")
    return CandidateGateContext(
        run_id="run-1",
        episode=1,
        active=active,
        candidate=candidate,
        active_root=active_root,
        candidate_root=candidate_root,
        adapter_name="test-adapter",
        events=(),
    )


def _runner(
    tmp_path: Path,
    executor: _Executor,
    *,
    family: SafetyCaseFamilyDefinition | None = None,
    model_config: LiveModelConfig | None = None,
    broker=None,
) -> GateRunner:
    return GateRunner(
        adapter=_Adapter(executor),
        suite=_Suite((family or _family(),)),
        controller_root=tmp_path / "controller",
        model_config=model_config,
        broker=broker,
    )


def _artifact_text(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _contains_key(value: object, forbidden: str) -> bool:
    if isinstance(value, dict):
        return forbidden in value or any(_contains_key(item, forbidden) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def test_gate_runs_every_declared_matched_cell_in_a_fresh_copy_and_publishes_terminal_artifacts(
    tmp_path: Path,
) -> None:
    arms = (
        EvaluationArm.MODEL_REFERENCE,
        EvaluationArm.FULL_HARNESS,
        EvaluationArm.MODULE_INTERVENTION,
    )
    strata = (EvidenceStratum.DETERMINISTIC_BOUNDARY, EvidenceStratum.ARCHIVE_LINEAGE)
    family = _family(strata=strata, arms=arms)
    executor = _Executor()
    runner = _runner(tmp_path, executor, family=family)
    context = _context(tmp_path)

    result = runner.evaluate(context)

    assert result == CandidateGateResult(
        True,
        "pass",
        "safety-gates/run-1/candidate-0001/decision.json",
    )
    assert {
        (endpoint, arm, stratum)
        for endpoint, arm, stratum, *_ in executor.calls
    } == {
        (endpoint, arm, stratum)
        for endpoint in ProbeEndpoint
        for arm in arms
        for stratum in strata
    }
    assert len({call[3] for call in executor.calls}) == 12
    assert len({call[4] for call in executor.calls}) == 12
    assert len({call[5] for call in executor.calls}) == 12
    assert not (context.active_root / "cell-marker.txt").exists()
    assert not (context.candidate_root / "cell-marker.txt").exists()

    safety_root = tmp_path / "controller" / "safety-gates"
    candidate_root = safety_root / "run-1" / "candidate-0001"
    assert (safety_root / "manifest.json").is_file()
    assert (safety_root / "run-1" / "activations.jsonl").is_file()
    assert not any((safety_root / "run-1" / ".staging").iterdir())
    assert all(
        (candidate_root / name).is_file()
        for name in (
            "transition.json",
            "observations.jsonl",
            "indicators.json",
            "decision.json",
        )
    )
    observations = [
        json.loads(line)
        for line in (candidate_root / "observations.jsonl").read_text().splitlines()
    ]
    assert all(not Path(ref).is_absolute() for row in observations for ref in row["evidence_refs"])
    payloads = [json.loads(path.read_text()) for path in candidate_root.glob("*.json")]
    payloads.extend(observations)
    assert not any(_contains_key(payload, "score") for payload in payloads)
    assert {field.name for field in fields(result)} == {"allowed", "status", "decision_ref"}


@pytest.mark.parametrize("location", ["active", "candidate", "run"])
def test_controller_root_must_be_outside_every_subject_root(tmp_path: Path, location: str) -> None:
    context = _context(tmp_path)
    roots = {
        "active": context.active_root,
        "candidate": context.candidate_root,
        "run": context.active_root.parent,
    }
    runner = _runner(tmp_path, _Executor())
    runner.controller_root = roots[location] / "controller"

    with pytest.raises(ValueError, match="outside active, candidate, and run roots"):
        runner.evaluate(context)

    assert not runner.controller_root.exists()


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [
        ("exception", "error"),
        ("wrong-type", "invalid"),
        ("missing-ref", "invalid"),
        ("missing-oracle", "invalid"),
        ("not-exposed", "not_evaluated"),
    ],
)
def test_executor_and_evidence_failures_are_published_rejections(
    tmp_path: Path, mode: str, expected_status: str
) -> None:
    result = _runner(tmp_path, _Executor(mode)).evaluate(_context(tmp_path))

    assert result.allowed is False
    assert result.status == expected_status
    assert result.decision_ref.endswith("/decision.json")
    artifacts = _artifact_text(tmp_path / "controller")
    assert "sk-secret-must-not-be-published" not in artifacts


def test_executor_timeout_is_an_error_rejection(tmp_path: Path) -> None:
    config = LiveModelConfig(model="gpt-fixed", timeout_seconds=0.01)

    result = _runner(tmp_path, _Executor("timeout"), model_config=config).evaluate(
        _context(tmp_path)
    )

    assert result.allowed is False
    assert result.status == "error"
    published_root = tmp_path / "controller" / "safety-gates"
    before = {
        path.relative_to(published_root).as_posix(): path.read_bytes()
        for path in published_root.rglob("*")
        if path.is_file()
    }
    time.sleep(0.08)
    after = {
        path.relative_to(published_root).as_posix(): path.read_bytes()
        for path in published_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_fixed_live_provenance_must_match_the_configured_model(tmp_path: Path) -> None:
    family = _family(strata=(EvidenceStratum.FIXED_LIVE_BEHAVIOR,))
    config = LiveModelConfig(model="gpt-fixed", timeout_seconds=0.1)

    result = _runner(
        tmp_path,
        _Executor("wrong-model"),
        family=family,
        model_config=config,
        broker=_Broker(),
    ).evaluate(_context(tmp_path))

    assert result.allowed is False
    assert result.status == "invalid"


def test_publication_failure_is_retained_and_never_indexed_as_passing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_publication(self, *, activation):
        raise OSError("publication unavailable")

    monkeypatch.setattr(AtomicCandidatePublication, "publish", fail_publication)
    runner = _runner(tmp_path, _Executor())

    result = runner.evaluate(_context(tmp_path))

    assert result.allowed is False
    assert result.status == "error"
    run_root = tmp_path / "controller" / "safety-gates" / "run-1"
    assert not (run_root / "candidate-0001").exists()
    assert not (run_root / "activations.jsonl").exists()
    assert list((run_root / ".failed").glob("candidate-0001-*"))
