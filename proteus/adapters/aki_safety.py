"""Candidate-local Phase 1 safety execution for the Aki adapter."""

from __future__ import annotations

import json
from pathlib import Path

from proteus.adapters.aki_live_worker import AkiWorkerController
from proteus.adapters.aki_safety_cases import ADMINISTRATORS
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
    ("loop.py", "file"),
    ("aki", "directory"),
    ("memory", "directory"),
    ("skills", "directory"),
    ("tools", "directory"),
)


def _evidence_ref(
    definition: SafetyCaseFamilyDefinition,
    endpoint: ProbeEndpoint,
    arm: EvaluationArm,
    stratum: EvidenceStratum,
    filename: str,
) -> str:
    return (
        Path("evidence")
        / definition.family_id
        / endpoint.value
        / arm.value
        / f"trial-{stratum.value}-0001"
        / filename
    ).as_posix()


class AkiCandidateSafetyExecutor:
    """Administer Phase 1 only through one materialized endpoint's native runtime."""

    name = "aki-candidate-safety"

    def __init__(self, worker: AkiWorkerController | None = None) -> None:
        self.worker = worker or AkiWorkerController()

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
            context.evidence_dir.mkdir(parents=True, exist_ok=True)
            filename = "canonical-snapshot-unavailable.json"
            (context.evidence_dir / filename).write_text(
                json.dumps(
                    {"status": "not_exposed", "missing": missing},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            ref = _evidence_ref(definition, endpoint, arm, stratum, filename)
            return ProbeObservation(
                snapshot=context.snapshot,
                endpoint=endpoint,
                arm=arm,
                stratum=stratum,
                exposure=SafetyExposure.NOT_EXPOSED,
                evidence_refs=(ref,),
                reason=f"canonical_aki_snapshot_missing:{','.join(missing)}",
            )
        administrator = ADMINISTRATORS.get(definition.family_id)
        if administrator is None:
            raise ValueError(
                f"Aki candidate safety does not administer family {definition.family_id!r}"
            )
        return administrator.collect(
            definition,
            endpoint,
            arm,
            stratum,
            context,
            channel,
            self.worker,
        )
