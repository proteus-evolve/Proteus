"""Candidate-local Phase 1 execution for the DSH adapter."""

from __future__ import annotations

from proteus.adapters.dsh_safety_cases import ADMINISTRATORS, DshSafetyRuntime, write_evidence
from proteus.safety.evidence import ProbeEndpoint, ProbeObservation
from proteus.safety.live import LiveModelChannel
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.taxonomy import (
    EvaluationArm,
    EvidenceStratum,
    SafetyCaseFamilyDefinition,
    SafetyExposure,
)

_REQUIRED_PATHS = (
    ("AGENTS.md", "file"),
    ("notes", "directory"),
    ("tools", "directory"),
    (".dsh/skills", "directory"),
    (".agents/skills", "directory"),
)


class DshCandidateSafetyExecutor:
    """Administer only the native surfaces exposed by one materialized DSH endpoint."""

    name = "dsh-candidate-safety"

    def __init__(self, runtime: DshSafetyRuntime | None = None) -> None:
        if runtime is None:
            from proteus.adapters.dsh import DshHeadlessRuntime

            runtime = DshHeadlessRuntime()
        self.runtime = runtime

    def collect(
        self,
        definition: SafetyCaseFamilyDefinition,
        endpoint: ProbeEndpoint,
        arm: EvaluationArm,
        stratum: EvidenceStratum,
        context: CandidateSafetyContext,
        channel: LiveModelChannel | None,
    ) -> ProbeObservation:
        missing = []
        for name, kind in _REQUIRED_PATHS:
            path = context.snapshot_root / name
            present = path.is_file() if kind == "file" else path.is_dir()
            if not present:
                missing.append(name)
        if missing:
            ref = write_evidence(
                definition,
                endpoint,
                arm,
                stratum,
                context,
                "canonical-snapshot-unavailable.json",
                {
                    "status": "not_exposed",
                    "missing": missing,
                    "required_roots": [name for name, _ in _REQUIRED_PATHS],
                },
            )
            return ProbeObservation(
                snapshot=context.snapshot,
                endpoint=endpoint,
                arm=arm,
                stratum=stratum,
                exposure=SafetyExposure.NOT_EXPOSED,
                evidence_refs=(ref,),
                reason=f"canonical_dsh_snapshot_missing:{','.join(missing)}",
            )
        administrator = ADMINISTRATORS.get(definition.family_id)
        if administrator is None:
            raise ValueError(
                f"DSH candidate safety does not administer family {definition.family_id!r}"
            )
        return administrator.collect(
            definition,
            endpoint,
            arm,
            stratum,
            context,
            channel,
            self.runtime,
        )
