import importlib
import json
from dataclasses import fields

import pytest

from proteus.core.adapter import ActionEvent, Surface
from proteus.safety.model import (
    TAXONOMY_VERSION,
    AuditAssessment,
    AuditCase,
    AuditContext,
    AuditMode,
    AuditObservation,
    AuditStatus,
    AuditSuite,
    AuditTaxonomy,
    CausalStatus,
    Exposure,
    build_result,
)


def taxonomy() -> AuditTaxonomy:
    return AuditTaxonomy(
        target="trace",
        scope="local",
        initiating_source="framework",
        episode_phases=("reflect",),
        evolution_stages=("committed_state",),
        failure_mode="evidence_integrity",
        evidence_authority="independent_oracle",
        evidence_method=AuditMode.ARTIFACT.value,
    )


def test_pass_requires_relative_evidence_and_no_failures() -> None:
    assessment = AuditAssessment(
        status=AuditStatus.PASS,
        exposure=Exposure.EXPOSED,
        observed_behavior=("trace was readable",),
        evidence_refs=("evidence/run-1/episode-1/trace/events.json",),
        observation=AuditObservation(
            detected_or_contained=True,
            causal_status=CausalStatus.NOT_EVALUATED,
        ),
    )
    assert json.loads(json.dumps(assessment.to_dict()))["status"] == "pass"


@pytest.mark.parametrize(
    "ref",
    ["", "   ", ".", "/tmp/evidence.json", "../evidence.json", "a/../../b"],
)
def test_evidence_refs_must_stay_under_audit_root(ref: str) -> None:
    with pytest.raises(ValueError, match="evidence references"):
        AuditAssessment(status=AuditStatus.PASS, evidence_refs=(ref,))


def test_fail_requires_concrete_failures() -> None:
    with pytest.raises(ValueError, match="failures"):
        AuditAssessment(
            status=AuditStatus.FAIL,
            evidence_refs=("evidence/failure.json",),
        )


def test_pass_requires_evidence() -> None:
    with pytest.raises(ValueError, match="evidence"):
        AuditAssessment(status=AuditStatus.PASS)


def test_error_requires_error_text() -> None:
    with pytest.raises(ValueError, match="error"):
        AuditAssessment(status=AuditStatus.ERROR)


def test_not_evaluated_is_not_a_pass() -> None:
    assessment = AuditAssessment(status=AuditStatus.NOT_EVALUATED)
    assert assessment.status is AuditStatus.NOT_EVALUATED
    assert assessment.status is not AuditStatus.PASS


def test_taxonomy_rejects_noncanonical_episode_phase() -> None:
    with pytest.raises(ValueError, match="episode phase"):
        AuditTaxonomy(
            target="trace",
            scope="local",
            initiating_source="framework",
            episode_phases=("deploy",),
            evolution_stages=("committed_state",),
            failure_mode="evidence_integrity",
            evidence_authority="independent_oracle",
            evidence_method="artifact",
        )


def test_build_result_binds_case_suite_and_context(tmp_path) -> None:
    class Case:
        case_id = "trace-readable"
        taxonomy = taxonomy()
        expected_behavior = ("trace is readable",)

    class Suite:
        name = "fixture"
        version = "1"

        def cases(self, adapter, surfaces):
            return (Case(),)

    context = AuditContext(
        audit_root=tmp_path / "audit",
        evidence_dir=tmp_path / "audit/evidence",
        run_id="run-1",
        adapter_name="minimal",
        arm="neutral",
        seed=0,
        episode=1,
        snapshot_root=tmp_path / "snapshot",
        surfaces=(Surface("notes", "notes"),),
        events=(ActionEvent(turn=1, phase="reflect", text="looks safe"),),
        self_assessments=("looks safe",),
    )
    assessment = AuditAssessment(status=AuditStatus.NOT_EVALUATED)

    result = build_result(
        suite=Suite(), case=Case(), context=context, assessment=assessment
    )

    assert result.taxonomy_version == TAXONOMY_VERSION
    assert result.suite == "fixture"
    assert result.case_id == "trace-readable"
    assert result.run_id == "run-1"
    assert result.self_assessments == ("looks safe",)


def test_audit_context_does_not_expose_source_run_root() -> None:
    assert "run_root" not in {item.name for item in fields(AuditContext)}


def test_obsolete_provider_and_measurement_evaluator_api_is_removed() -> None:
    from proteus import safety
    from proteus.safety import model, plugins, taxonomy

    for module, names in (
        (model, ("SafetyEvidenceProvider", "SafetyEvidenceAdapter")),
        (
            plugins,
            (
                "HarnessDecision",
                "HarnessSafetyAdapter",
                "HarnessSafetyContext",
                "HarnessSafetyEvidence",
                "HarnessSafetyEvidenceProvider",
                "ModelBehavior",
                "ModuleObservation",
                "ResponsibilityObservation",
            ),
        ),
        (
            taxonomy,
            (
                "HarnessContribution",
                "MODULE_SAFETY_TAXONOMY_VERSION",
                "ModuleCausalStatus",
                "TransitionDirection",
            ),
        ),
        (safety, (
            "FamilyAssessment",
            "HarnessContribution",
            "HarnessDecision",
            "HarnessSafetyAdapter",
            "HarnessSafetyContext",
            "HarnessSafetyEvidence",
            "SafetyEvidenceProvider",
            "SafetyEvidenceAdapter",
            "SafetyMeasurementEvaluator",
            "run_harness_safety",
            "HarnessSafetyEvidenceProvider",
            "MODULE_SAFETY_TAXONOMY_VERSION",
            "ModelBehavior",
            "ModuleCausalStatus",
            "ModuleObservation",
            "ModuleSafetyCaseSuite",
            "ResponsibilityObservation",
            "TransitionDirection",
            "evaluate_family",
            "implemented_case_families",
        )),
    ):
        assert all(not hasattr(module, name) for name in names)
    for module_name in (
        "proteus.safety.cases",
        "proteus.safety.evaluation",
        "proteus.safety.evaluator",
        "proteus.safety.runtime",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_name)


def test_instrument_integrity_audit_contract_remains_public() -> None:
    from proteus import safety

    assert safety.AuditCase is AuditCase
    assert safety.AuditSuite is AuditSuite
    assert callable(safety.run_audit)
    assert safety.load_suite("proteus.safety.integrity:SUITE").name == "instrument-integrity"
