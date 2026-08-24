from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import FunctionType, MethodType

import pytest

from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import (
    BoundaryDecision,
    ContainmentOutcome,
    IncidentObservation,
    PermissionObservation,
    ProbeEndpoint,
    ProbeObservation,
    ProbeStatuses,
    ProposalEffectObservation,
    StageValue,
    UnsafeStateObservation,
)
from proteus.safety.harness_loading import preflight_harness_safety_suite
from proteus.safety.live import (
    LiveCallProvenance,
    LiveCellBudget,
    LiveModelBroker,
    LiveModelConfig,
)
from proteus.safety.phase1 import SUITE
from proteus.safety.taxonomy import (
    EvidenceStratum,
    HarnessModule,
    SafetyStatus,
)


def _snapshot(role: SnapshotRole = SnapshotRole.CANDIDATE) -> SnapshotRef:
    return SnapshotRef(run_id="run-1", episode=1, role=role)


def _fixed_live_observation(
    provenance: tuple[LiveCallProvenance, ...],
) -> ProbeObservation:
    return ProbeObservation(
        snapshot=_snapshot(),
        endpoint=ProbeEndpoint.CANDIDATE,
        arm=SUITE.definitions()[0].evaluation_arms[0],
        stratum=EvidenceStratum.FIXED_LIVE_BEHAVIOR,
        statuses=ProbeStatuses(behavior=SafetyStatus.PASS),
        model_provenance=provenance,
        evidence_refs=("evidence/live.json",),
    )


def test_phase1_declares_exactly_three_fixed_primary_families() -> None:
    families = {family.family_id: family for family in SUITE.definitions()}

    assert set(families) == {
        "memory_bad_admission",
        "memory_collapse",
        "tools_permission_drift",
    }
    assert families["memory_bad_admission"].primary_module is HarnessModule.MEMORY
    assert families["memory_collapse"].primary_module is HarnessModule.MEMORY
    assert families["tools_permission_drift"].primary_module is HarnessModule.TOOLS
    assert families["tools_permission_drift"].supporting_modules == (
        HarnessModule.SKILLS,
        HarnessModule.AGENT_LOOP,
    )


def test_phase1_critical_requirements_declare_evidence_strata() -> None:
    for family in SUITE.definitions():
        assert family.family_version == "1"
        assert family.invariant.invariant_id.startswith(f"{family.family_id}.")
        assert family.exposure_rule
        assert 0.0 <= family.utility_minimum <= 1.0
        assert all(
            requirement.required_strata
            for requirement in family.indicator_requirements
            if requirement.critical
        )


def test_unknown_propagation_stages_remain_typed_unknown() -> None:
    observation = UnsafeStateObservation(
        state_id="state-1",
        parent_state_id=None,
    )

    assert observation.boundary_exposure is StageValue.UNKNOWN
    assert observation.state_commitment is StageValue.UNKNOWN
    assert observation.retrieval_or_activation is StageValue.UNKNOWN
    assert observation.decision_influence is StageValue.UNKNOWN
    assert observation.operation_attempt is StageValue.UNKNOWN
    assert observation.harm_commit is StageValue.UNKNOWN
    assert observation.candidate_lineage_persistence is StageValue.UNKNOWN
    assert observation.active_lineage_persistence is StageValue.UNKNOWN


def test_authorization_and_direct_reachability_are_independent() -> None:
    observation = PermissionObservation(
        effect_id="effect-1",
        externally_authorized=StageValue.FALSE,
        effective_reachability=StageValue.TRUE,
    )

    assert observation.externally_authorized is StageValue.FALSE
    assert observation.effective_reachability is StageValue.TRUE


def test_containment_requires_observed_proposal_and_effect_identity() -> None:
    with pytest.raises(ValueError, match="proposal and effect identity"):
        ProposalEffectObservation(
            proposal_id=None,
            effect_id=None,
            boundary_opportunity=StageValue.TRUE,
            decision=BoundaryDecision.DENY,
            containment_outcome=ContainmentOutcome.HARNESS_CONTAINED,
        )


def test_verified_recovery_requires_a_direct_relative_reference() -> None:
    with pytest.raises(ValueError, match="verification reference"):
        IncidentObservation(
            incident_id="incident-1",
            recovery_target="state-before-trial",
            evaluator_observed=StageValue.TRUE,
            state_restored=StageValue.TRUE,
        )

    with pytest.raises(ValueError, match="relative"):
        IncidentObservation(
            incident_id="incident-1",
            recovery_target="state-before-trial",
            evaluator_observed=StageValue.TRUE,
            state_restored=StageValue.TRUE,
            verification_ref="/tmp/restored.json",
        )

    incident = IncidentObservation(
        incident_id="incident-1",
        recovery_target="state-before-trial",
        evaluator_observed=StageValue.TRUE,
        state_restored=StageValue.TRUE,
        verification_ref="evidence/restored.json",
        verified_safe_episode=2,
    )
    assert incident.verified_safe_episode == 2


def test_verified_safe_episode_rejects_non_integer_values() -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        IncidentObservation(
            incident_id="incident-1",
            recovery_target="state-before-trial",
            evaluator_observed=StageValue.TRUE,
            state_restored=StageValue.TRUE,
            verification_ref="evidence/restored.json",
            verified_safe_episode=1.5,
        )


def test_deterministic_evidence_cannot_establish_model_behavior() -> None:
    with pytest.raises(ValueError, match="deterministic.*model behavior"):
        ProbeObservation(
            snapshot=_snapshot(),
            endpoint=ProbeEndpoint.CANDIDATE,
            arm=SUITE.definitions()[0].evaluation_arms[0],
            stratum=EvidenceStratum.DETERMINISTIC_BOUNDARY,
            statuses=ProbeStatuses(behavior=SafetyStatus.PASS),
            evidence_refs=("evidence/boundary.json",),
        )


def test_deterministic_evidence_cannot_claim_containment() -> None:
    with pytest.raises(ValueError, match="deterministic.*containment"):
        ProbeObservation(
            snapshot=_snapshot(),
            endpoint=ProbeEndpoint.CANDIDATE,
            arm=SUITE.definitions()[0].evaluation_arms[0],
            stratum=EvidenceStratum.DETERMINISTIC_BOUNDARY,
            proposal_effects=(
                ProposalEffectObservation(
                    proposal_id="proposal-1",
                    effect_id="effect-1",
                    boundary_opportunity=StageValue.TRUE,
                    decision=BoundaryDecision.DENY,
                    containment_outcome=ContainmentOutcome.HARNESS_CONTAINED,
                ),
            ),
        )


def test_suite_preflight_rejects_provider_bearing_suites() -> None:
    class ProviderBearingSuite:
        name = "provider-bearing"
        version = "1"

        def definitions(self):
            return SUITE.definitions()

        def provider(self):
            return object()

    with pytest.raises(TypeError, match="definitions-only"):
        preflight_harness_safety_suite(
            ProviderBearingSuite(),
            model_config=None,
            repository_root=Path.cwd(),
        )


def test_fixed_live_preflight_reads_only_the_repository_root_credential(
    tmp_path: Path,
) -> None:
    config = LiveModelConfig(model="gpt-5.6-luna")

    with pytest.raises(ValueError, match="explicit model config"):
        preflight_harness_safety_suite(
            SUITE,
            model_config=None,
            repository_root=tmp_path,
        )

    with pytest.raises(ValueError, match="credential file is missing"):
        preflight_harness_safety_suite(
            SUITE,
            model_config=config,
            repository_root=tmp_path,
        )

    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-repository-secret\n")
    definitions = preflight_harness_safety_suite(
        SUITE,
        model_config=config,
        repository_root=tmp_path,
    )
    assert definitions == SUITE.definitions()


def test_fixed_live_evidence_requires_matching_model_provenance() -> None:
    with pytest.raises(ValueError, match="matching model provenance"):
        _fixed_live_observation(())

    with pytest.raises(ValueError, match="matching model provenance"):
        _fixed_live_observation(
            (
                LiveCallProvenance(
                    call_id="call-1",
                    response_id="resp-1",
                    configured_model="gpt-5.6-luna",
                    response_model="gpt-5.6-sol",
                ),
            )
        )


def test_live_configuration_and_provenance_never_serialize_credentials() -> None:
    config = LiveModelConfig(
        model="gpt-5.6-luna",
        credential_env="OPENAI_API_KEY",
        budget=LiveCellBudget(max_calls=2, max_output_tokens=800),
    )
    observation = _fixed_live_observation(
        (
            LiveCallProvenance(
                call_id="call-1",
                response_id="resp-1",
                configured_model="gpt-5.6-luna",
                response_model="gpt-5.6-luna",
            ),
        )
    )

    serialized = repr({"config": asdict(config), "observation": asdict(observation)})
    assert "sk-secret" not in serialized
    assert "credential" not in asdict(config)


def test_broker_normalizes_responses_and_owns_each_cell_budget() -> None:
    class FixtureTransport:
        def __init__(self) -> None:
            self.credentials: list[str] = []

        def create(self, *, credential, payload, **kwargs):
            del kwargs
            self.credentials.append(credential)
            assert payload["model"] == "gpt-5.6-luna"
            assert payload["max_output_tokens"] == 800
            return {
                "id": "resp-1",
                "model": "gpt-5.6-luna",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "bounded"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "tool-1",
                        "name": "record_effect",
                        "arguments": '{"effect_id":"effect-1"}',
                    },
                ],
            }

    transport = FixtureTransport()
    broker = LiveModelBroker(
        LiveModelConfig(
            model="gpt-5.6-luna",
            budget=LiveCellBudget(max_calls=1, max_output_tokens=800),
        ),
        "sk-secret",
        transport=transport,
    )
    channel = broker.channel("candidate-cell")

    assert "sk-secret" not in _reachable_strings_from_channel(channel)

    response = channel.respond(input="controlled prompt")

    assert response.output_text == "bounded"
    assert response.tool_calls[0].arguments == {"effect_id": "effect-1"}
    assert response.provenance.configured_model == "gpt-5.6-luna"
    assert "sk-secret" not in repr(asdict(response))
    assert transport.credentials == ["sk-secret"]
    assert "sk-secret" not in _reachable_strings_from_channel(channel)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        channel.respond(input="second call")


def _reachable_strings_from_channel(channel: object) -> set[str]:
    """Traverse Python-level state and callable captures reachable from a channel."""
    pending = list(vars(channel).values())
    seen: set[int] = set()
    strings: set[str] = set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, str):
            strings.add(value)
        elif isinstance(value, MethodType):
            pending.extend((value.__self__, value.__func__))
        elif isinstance(value, FunctionType):
            pending.extend(
                cell.cell_contents for cell in (value.__closure__ or ())
            )
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
        elif hasattr(value, "__dict__"):
            pending.extend(vars(value).values())
    return strings
