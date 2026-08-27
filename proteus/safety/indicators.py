"""Pure matched-transition projections for activation safety."""

from __future__ import annotations

from dataclasses import dataclass

from proteus.safety.evidence import ProbeObservation, ProbeStatuses
from proteus.safety.permission_evidence import (
    PermissionComparisonStatus,
    PermissionEvidenceValidity,
    PermissionFamilyComparison,
)
from proteus.safety.taxonomy import SafetyStatus


@dataclass(frozen=True)
class MatchedFamilyObservations:
    active: ProbeObservation
    candidate: ProbeObservation
    family_version: str

    def __post_init__(self) -> None:
        if self.active.family_id != self.candidate.family_id:
            raise ValueError("matched safety observations require one family")
        if self.active.snapshot.run_id != self.candidate.snapshot.run_id:
            raise ValueError("matched safety observations require one run")


@dataclass(frozen=True)
class FamilyIndicatorProjection:
    family_id: str
    family_version: str
    terminal_status: SafetyStatus
    active_status: SafetyStatus | None
    candidate_status: SafetyStatus | None
    comparison_status: PermissionComparisonStatus | None
    evidence_validity: PermissionEvidenceValidity | None
    active_components: ProbeStatuses | None
    candidate_components: ProbeStatuses | None
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        def components(value: ProbeStatuses | None) -> dict[str, str] | None:
            if value is None:
                return None
            return {
                "module": value.module.value,
                "behavior": value.behavior.value,
                "utility": value.utility.value,
                "authorization": value.authorization.value,
                "recovery": value.recovery.value,
            }

        return {
            "family_id": self.family_id,
            "family_version": self.family_version,
            "terminal_status": self.terminal_status.value,
            "active_status": (
                self.active_status.value if self.active_status is not None else None
            ),
            "candidate_status": (
                self.candidate_status.value
                if self.candidate_status is not None
                else None
            ),
            "comparison_status": (
                self.comparison_status.value
                if self.comparison_status is not None
                else None
            ),
            "evidence_validity": (
                self.evidence_validity.value
                if self.evidence_validity is not None
                else None
            ),
            "active_components": components(self.active_components),
            "candidate_components": components(self.candidate_components),
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class EvolutionSafetyIndicators:
    families: tuple[FamilyIndicatorProjection, ...]

    def to_dict(self) -> dict[str, object]:
        return {"families": [family.to_dict() for family in self.families]}


def derive_indicator_profile(
    pairs: tuple[MatchedFamilyObservations, ...],
    permission: PermissionFamilyComparison,
) -> EvolutionSafetyIndicators:
    return EvolutionSafetyIndicators(
        (
            *(
            FamilyIndicatorProjection(
                family_id=pair.candidate.family_id,
                family_version=pair.family_version,
                terminal_status=pair.candidate.status,
                active_status=pair.active.status,
                candidate_status=pair.candidate.status,
                comparison_status=None,
                evidence_validity=None,
                active_components=pair.active.statuses,
                candidate_components=pair.candidate.statuses,
            )
            for pair in pairs
            ),
            FamilyIndicatorProjection(
                family_id=permission.family_id,
                family_version=permission.family_version,
                terminal_status=permission.terminal_status,
                active_status=None,
                candidate_status=None,
                comparison_status=permission.comparison_status,
                evidence_validity=permission.validity,
                active_components=None,
                candidate_components=None,
                blockers=permission.blockers,
            ),
        )
    )
