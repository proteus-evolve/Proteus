"""Pure matched-transition projections for activation safety."""

from __future__ import annotations

from dataclasses import dataclass

from proteus.safety.evidence import ProbeObservation, ProbeStatuses
from proteus.safety.taxonomy import SafetyStatus


@dataclass(frozen=True)
class MatchedFamilyObservations:
    active: ProbeObservation
    candidate: ProbeObservation

    def __post_init__(self) -> None:
        if self.active.family_id != self.candidate.family_id:
            raise ValueError("matched safety observations require one family")
        if self.active.snapshot.run_id != self.candidate.snapshot.run_id:
            raise ValueError("matched safety observations require one run")


@dataclass(frozen=True)
class FamilyIndicatorProjection:
    family_id: str
    active_status: SafetyStatus
    candidate_status: SafetyStatus
    active_components: ProbeStatuses
    candidate_components: ProbeStatuses

    def to_dict(self) -> dict[str, object]:
        def components(value: ProbeStatuses) -> dict[str, str]:
            return {
                "module": value.module.value,
                "behavior": value.behavior.value,
                "utility": value.utility.value,
                "authorization": value.authorization.value,
                "recovery": value.recovery.value,
            }

        return {
            "family_id": self.family_id,
            "active_status": self.active_status.value,
            "candidate_status": self.candidate_status.value,
            "active_components": components(self.active_components),
            "candidate_components": components(self.candidate_components),
        }


@dataclass(frozen=True)
class EvolutionSafetyIndicators:
    families: tuple[FamilyIndicatorProjection, ...]

    def to_dict(self) -> dict[str, object]:
        return {"families": [family.to_dict() for family in self.families]}


def derive_indicator_profile(
    pairs: tuple[MatchedFamilyObservations, ...],
) -> EvolutionSafetyIndicators:
    return EvolutionSafetyIndicators(
        tuple(
            FamilyIndicatorProjection(
                family_id=pair.candidate.family_id,
                active_status=pair.active.status,
                candidate_status=pair.candidate.status,
                active_components=pair.active.statuses,
                candidate_components=pair.candidate.statuses,
            )
            for pair in pairs
        )
    )
