"""Matched candidate safety execution and activation publication."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from proteus.core.activation import CandidateGateContext, CandidateGateResult
from proteus.safety.evidence import ProbeEndpoint, ProbeObservation, ProbeStatuses
from proteus.safety.harness_loading import validate_harness_safety_suite
from proteus.safety.indicators import MatchedProbeObservations, derive_indicator_profile
from proteus.safety.live import LiveModelBroker, LiveModelChannel, LiveModelConfig
from proteus.safety.plugins import (
    CandidateSafetyAdapter,
    CandidateSafetyContext,
    HarnessSafetyCaseSuite,
)
from proteus.safety.policy import evaluate_safety_policy
from proteus.safety.publication import AtomicCandidatePublication
from proteus.safety.taxonomy import (
    EvidenceStratum,
    SafetyCaseFamilyDefinition,
    SafetyIndicator,
    SafetyStatus,
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _all_evidence_refs(observation: ProbeObservation) -> tuple[str, ...]:
    refs = set(observation.evidence_refs)
    for group in (
        observation.invariants,
        observation.unsafe_states,
        observation.permissions,
        observation.proposal_effects,
        observation.utilities,
        observation.incidents,
    ):
        for item in group:
            refs.update(item.evidence_refs)
    refs.update(
        incident.verification_ref
        for incident in observation.incidents
        if incident.verification_ref is not None
    )
    return tuple(sorted(refs))


def _source_status(observation: ProbeObservation, indicator: SafetyIndicator) -> SafetyStatus:
    if indicator is SafetyIndicator.INVARIANT_PRESERVATION:
        return observation.statuses.module
    if indicator is SafetyIndicator.UNSAFE_STATE_PROPAGATION:
        return (
            observation.statuses.behavior
            if observation.stratum is EvidenceStratum.FIXED_LIVE_BEHAVIOR
            else observation.statuses.module
        )
    if indicator is SafetyIndicator.PERMISSION_BOUNDARY_INTEGRITY:
        return observation.statuses.authorization
    if indicator is SafetyIndicator.HARM_CONTAINMENT:
        return observation.statuses.behavior
    return observation.statuses.recovery


def _validate_boundary_oracles(
    definition: SafetyCaseFamilyDefinition, observation: ProbeObservation
) -> None:
    if observation.stratum is not EvidenceStratum.DETERMINISTIC_BOUNDARY:
        return
    required_groups = {
        SafetyIndicator.INVARIANT_PRESERVATION: (observation.invariants,),
        SafetyIndicator.UNSAFE_STATE_PROPAGATION: (observation.unsafe_states,),
        SafetyIndicator.PERMISSION_BOUNDARY_INTEGRITY: (
            observation.effects,
            observation.permissions,
        ),
        SafetyIndicator.RECOVERY_ROLLBACK: (observation.incidents,),
    }
    for requirement in definition.indicator_requirements:
        if observation.stratum not in requirement.required_strata:
            continue
        if _source_status(observation, requirement.indicator) not in {
            SafetyStatus.PASS,
            SafetyStatus.FAIL,
        }:
            continue
        if requirement.indicator is SafetyIndicator.INVARIANT_PRESERVATION:
            if not any(
                item.invariant_id == definition.invariant.invariant_id
                for item in observation.invariants
            ):
                raise ValueError(
                    "deterministic boundary evidence lacks the declared invariant oracle"
                )
            continue
        if any(not group for group in required_groups.get(requirement.indicator, ())):
            raise ValueError(
                f"deterministic boundary evidence lacks a direct {requirement.indicator.value} oracle"
            )


def _validate_observation(
    observation: object,
    *,
    definition: SafetyCaseFamilyDefinition,
    endpoint: ProbeEndpoint,
    arm,
    stratum: EvidenceStratum,
    context: CandidateSafetyContext,
    evidence_ref_root: Path,
    model_config: LiveModelConfig | None,
) -> ProbeObservation:
    if not isinstance(observation, ProbeObservation):
        raise TypeError("candidate safety executor returned malformed evidence")
    if (
        observation.snapshot != context.snapshot
        or observation.endpoint is not endpoint
        or observation.arm is not arm
        or observation.stratum is not stratum
    ):
        raise ValueError("candidate safety executor returned mismatched evidence identity")
    for ref in _all_evidence_refs(observation):
        try:
            relative = Path(ref).relative_to(evidence_ref_root)
        except ValueError as exc:
            raise ValueError(
                "candidate safety evidence reference is not a direct cell artifact"
            ) from exc
        if not (context.evidence_dir / relative).is_file():
            raise ValueError("candidate safety evidence reference is not a direct cell artifact")
    if stratum is EvidenceStratum.FIXED_LIVE_BEHAVIOR:
        if model_config is None:
            raise ValueError("fixed-live evidence requires an explicit model config")
        if any(item.configured_model != model_config.model for item in observation.model_provenance):
            raise ValueError("fixed-live provenance does not match the configured model")
    _validate_boundary_oracles(definition, observation)
    return observation


def _terminal_observation(
    *,
    definition: SafetyCaseFamilyDefinition,
    endpoint: ProbeEndpoint,
    arm,
    stratum: EvidenceStratum,
    context: CandidateSafetyContext,
    artifact_root: Path,
    evidence_dir: Path,
    status: SafetyStatus,
    code: str,
    configured_model: str | None,
) -> ProbeObservation:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    failure_path = evidence_dir / "failure.json"
    _write_json(
        failure_path,
        {"status": status.value, "code": code, "configured_model": configured_model},
    )
    ref = failure_path.relative_to(artifact_root).as_posix()
    statuses = ProbeStatuses(
        module=status,
        behavior=(
            SafetyStatus.NOT_EVALUATED
            if stratum is EvidenceStratum.DETERMINISTIC_BOUNDARY
            else status
        ),
        utility=status,
        authorization=status,
        recovery=status,
    )
    return ProbeObservation(
        snapshot=context.snapshot,
        endpoint=endpoint,
        arm=arm,
        stratum=stratum,
        statuses=statuses,
        evidence_refs=(ref,),
        reason=code,
    )


def _close_live_channel(
    channel: LiveModelChannel | None,
    broker: LiveModelBroker | None,
) -> None:
    """Close a broker channel and wait for its provider worker when supported."""
    if channel is None:
        return
    close_channel = getattr(broker, "close_channel", None)
    if callable(close_channel):
        close_channel(channel)
        return
    close = getattr(channel, "close", None)
    if callable(close):
        close()


@dataclass
class GateRunner:
    adapter: CandidateSafetyAdapter
    suite: HarnessSafetyCaseSuite
    controller_root: Path
    model_config: LiveModelConfig | None = None
    broker: LiveModelBroker | None = None

    def _validate_controller_root(self, context: CandidateGateContext) -> None:
        controller = self.controller_root.resolve()
        subject_roots = [context.active_root.resolve(), context.candidate_root.resolve()]
        if context.active_root.parent.resolve() == context.candidate_root.parent.resolve():
            subject_roots.append(context.active_root.parent.resolve())
        if any(
            controller == root
            or controller.is_relative_to(root)
            or root.is_relative_to(controller)
            for root in subject_roots
        ):
            raise ValueError("controller_root must be outside active, candidate, and run roots")

    def _collect(
        self,
        *,
        executor,
        definition: SafetyCaseFamilyDefinition,
        endpoint: ProbeEndpoint,
        arm,
        stratum: EvidenceStratum,
        gate_context: CandidateGateContext,
        profile,
        artifact_root: Path,
    ) -> ProbeObservation:
        snapshot = gate_context.active if endpoint is ProbeEndpoint.ACTIVE else gate_context.candidate
        source = (
            gate_context.active_root
            if endpoint is ProbeEndpoint.ACTIVE
            else gate_context.candidate_root
        )
        evidence_ref_root = (
            Path("evidence")
            / definition.family_id
            / endpoint.value
            / arm.value
            / f"trial-{stratum.value}-0001"
        )
        published_evidence_dir = artifact_root / evidence_ref_root
        with tempfile.TemporaryDirectory(prefix="proteus-safety-cell-") as temporary:
            trial_root = Path(temporary)
            snapshot_root = trial_root / "snapshot"
            local_evidence_dir = trial_root / "evidence"
            shutil.copytree(source, snapshot_root)
            context = CandidateSafetyContext(
                run_id=gate_context.run_id,
                episode=gate_context.episode,
                adapter_name=gate_context.adapter_name,
                snapshot=snapshot,
                snapshot_root=snapshot_root,
                trial_root=trial_root,
                evidence_dir=local_evidence_dir,
                profile=profile,
                events=gate_context.events,
                controller_root=self.controller_root,
            )
            if stratum is EvidenceStratum.FIXED_LIVE_BEHAVIOR and (
                self.model_config is None or self.broker is None
            ):
                return _terminal_observation(
                    definition=definition,
                    endpoint=endpoint,
                    arm=arm,
                    stratum=stratum,
                    context=context,
                    artifact_root=artifact_root,
                    evidence_dir=published_evidence_dir,
                    status=SafetyStatus.ERROR,
                    code="fixed_live_broker_unavailable",
                    configured_model=(
                        self.model_config.model if self.model_config is not None else None
                    ),
                )
            channel = None
            if stratum is EvidenceStratum.FIXED_LIVE_BEHAVIOR:
                cell_id = (
                    f"{definition.family_id}.{endpoint.value}.{arm.value}."
                    f"{stratum.value}.trial-0001"
                )
                channel = self.broker.channel(cell_id) if self.broker is not None else None
            try:
                try:
                    observation = executor.collect(
                        definition, endpoint, arm, stratum, context, channel
                    )
                finally:
                    _close_live_channel(channel, self.broker)
            except TimeoutError:
                return _terminal_observation(
                    definition=definition,
                    endpoint=endpoint,
                    arm=arm,
                    stratum=stratum,
                    context=context,
                    artifact_root=artifact_root,
                    evidence_dir=published_evidence_dir,
                    status=SafetyStatus.ERROR,
                    code="executor_timeout",
                    configured_model=(
                        self.model_config.model if self.model_config is not None else None
                    ),
                )
            except Exception:  # noqa: BLE001 - raw executor exceptions are not publishable
                return _terminal_observation(
                    definition=definition,
                    endpoint=endpoint,
                    arm=arm,
                    stratum=stratum,
                    context=context,
                    artifact_root=artifact_root,
                    evidence_dir=published_evidence_dir,
                    status=SafetyStatus.ERROR,
                    code="executor_exception",
                    configured_model=(
                        self.model_config.model if self.model_config is not None else None
                    ),
                )
            try:
                validated = _validate_observation(
                    observation,
                    definition=definition,
                    endpoint=endpoint,
                    arm=arm,
                    stratum=stratum,
                    context=context,
                    evidence_ref_root=evidence_ref_root,
                    model_config=self.model_config,
                )
                if local_evidence_dir.exists():
                    shutil.copytree(local_evidence_dir, published_evidence_dir)
                return validated
            except (TypeError, ValueError):
                return _terminal_observation(
                    definition=definition,
                    endpoint=endpoint,
                    arm=arm,
                    stratum=stratum,
                    context=context,
                    artifact_root=artifact_root,
                    evidence_dir=published_evidence_dir,
                    status=SafetyStatus.INVALID,
                    code="malformed_evidence",
                    configured_model=(
                        self.model_config.model if self.model_config is not None else None
                    ),
                )

    def evaluate(self, context: CandidateGateContext) -> CandidateGateResult:
        self._validate_controller_root(context)
        definitions = validate_harness_safety_suite(self.suite)
        profile = self.adapter.harness_safety_profile()
        executor = self.adapter.candidate_safety_executor()
        safety_root = self.controller_root / "safety-gates"
        candidate_id = f"candidate-{context.episode:04d}"
        publication = AtomicCandidatePublication(safety_root, context.run_id, candidate_id)
        try:
            with publication:
                assert publication.staging_root is not None
                staging = publication.staging_root
                observations: dict[str, tuple[MatchedProbeObservations, ...]] = {}
                flat_observations: list[dict[str, object]] = []
                for definition in definitions:
                    pairs = []
                    strata = tuple(
                        dict.fromkeys(
                            stratum
                            for requirement in definition.indicator_requirements
                            for stratum in requirement.required_strata
                        )
                    )
                    for arm in definition.evaluation_arms:
                        for stratum in strata:
                            active = self._collect(
                                executor=executor,
                                definition=definition,
                                endpoint=ProbeEndpoint.ACTIVE,
                                arm=arm,
                                stratum=stratum,
                                gate_context=context,
                                profile=profile,
                                artifact_root=staging,
                            )
                            candidate = self._collect(
                                executor=executor,
                                definition=definition,
                                endpoint=ProbeEndpoint.CANDIDATE,
                                arm=arm,
                                stratum=stratum,
                                gate_context=context,
                                profile=profile,
                                artifact_root=staging,
                            )
                            pairs.append(MatchedProbeObservations(active, candidate))
                            flat_observations.extend(
                                (
                                    {"family_id": definition.family_id, **_jsonable(active)},
                                    {"family_id": definition.family_id, **_jsonable(candidate)},
                                )
                            )
                    observations[definition.family_id] = tuple(pairs)
                indicator_profile = derive_indicator_profile(
                    active=context.active,
                    candidate=context.candidate,
                    families=definitions,
                    observations=observations,
                )
                decision = evaluate_safety_policy(
                    indicator_profile, definitions, observations
                )
                decision_ref = (
                    Path("safety-gates")
                    / context.run_id
                    / candidate_id
                    / "decision.json"
                ).as_posix()
                _write_json(
                    staging / "transition.json",
                    {
                        "run_id": context.run_id,
                        "episode": context.episode,
                        "adapter": context.adapter_name,
                        "configured_model": (
                            self.model_config.model if self.model_config is not None else None
                        ),
                        "active": context.active.to_dict(),
                        "candidate": context.candidate.to_dict(),
                        "suite": {"name": self.suite.name, "version": self.suite.version},
                    },
                )
                (staging / "observations.jsonl").write_text(
                    "".join(
                        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                        for row in flat_observations
                    ),
                    encoding="utf-8",
                )
                _write_json(staging / "indicators.json", indicator_profile.to_dict())
                _write_json(staging / "decision.json", decision.to_dict())
                publication.publish(
                    activation={
                        "episode": context.episode,
                        "candidate": context.candidate.to_dict(),
                        "allowed": decision.allowed,
                        "status": decision.status.value,
                        "decision_ref": decision_ref,
                    }
                )
        except Exception:  # noqa: BLE001 - publication failure is a gate rejection
            failed_ref = ""
            if publication.failed_root is not None:
                _write_json(
                    publication.failed_root / "decision.json",
                    {
                        "status": "error",
                        "allowed": False,
                        "blockers": [{"code": "publication_failure"}],
                        "warnings": [],
                    },
                )
                failed_ref = (
                    publication.failed_root.relative_to(self.controller_root)
                    / "decision.json"
                ).as_posix()
            return CandidateGateResult(False, "error", failed_ref)
        return CandidateGateResult(decision.allowed, decision.status.value, decision_ref)
