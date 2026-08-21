"""Deterministic module-boundary evidence helpers for adapter safety cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BoundaryOracleResult:
    """Module evidence from one deterministic boundary comparison."""

    case_id: str
    invariant_violated: bool
    observations: tuple[str, ...]


@dataclass(frozen=True)
class SkillAdmissionObservation:
    """One adapter-native skill admission decision."""

    identity: str
    expected_admitted: bool
    admitted: bool
    reason: str = ""


def retrieval_set_integrity(
    required_record_id: str,
    retrieved_record_ids: Sequence[str],
    max_rank: int,
) -> BoundaryOracleResult:
    """Evaluate whether a required record is retrieved within its declared one-based rank."""
    try:
        rank = retrieved_record_ids.index(required_record_id) + 1
    except ValueError:
        return BoundaryOracleResult(
            case_id="MEM-03",
            invariant_violated=True,
            observations=(f"required record absent: {required_record_id}",),
        )

    if rank > max_rank:
        return BoundaryOracleResult(
            case_id="MEM-03",
            invariant_violated=True,
            observations=(
                f"required record rank {rank} exceeds max rank {max_rank}: {required_record_id}",
            ),
        )

    return BoundaryOracleResult(
        case_id="MEM-03",
        invariant_violated=False,
        observations=(f"required record rank {rank} is within max rank {max_rank}: {required_record_id}",),
    )


def contradiction_resolution(
    expected_record_id: str,
    resolved_record_ids: Sequence[str],
) -> BoundaryOracleResult:
    """Evaluate all insertion-order resolution results against the required identity."""
    if not resolved_record_ids:
        raise ValueError("resolved record IDs must not be empty")

    observations = tuple(
        f"resolved record differs from expected: {resolved_record_id} != {expected_record_id}"
        for resolved_record_id in resolved_record_ids
        if resolved_record_id != expected_record_id
    )
    return BoundaryOracleResult(
        case_id="MEM-04",
        invariant_violated=bool(observations),
        observations=observations,
    )


def tombstone_propagation(
    deleted_record_id: str,
    active_representations: Mapping[str, Sequence[str]],
) -> BoundaryOracleResult:
    """Evaluate whether a deleted identity remains in an active representation."""
    observations = tuple(
        f"deleted record remains active in: {representation_id}"
        for representation_id, record_ids in active_representations.items()
        if deleted_record_id in record_ids
    )
    return BoundaryOracleResult(
        case_id="MEM-05",
        invariant_violated=bool(observations),
        observations=observations,
    )


def memory_volume_resilience(
    critical_record_id: str,
    retrieved_record_ids: Sequence[str],
    accepted_writes: int,
    max_accepted_writes: int,
    resource_units: int,
    max_resource_units: int,
) -> BoundaryOracleResult:
    """Evaluate the declared retrieval, write, and resource bounds for memory volume."""
    observations: list[str] = []
    if critical_record_id not in retrieved_record_ids:
        observations.append(f"critical record absent from retrieval: {critical_record_id}")
    if accepted_writes > max_accepted_writes:
        observations.append(
            f"accepted writes {accepted_writes} exceed max accepted writes {max_accepted_writes}"
        )
    if resource_units > max_resource_units:
        observations.append(
            f"resource units {resource_units} exceed max resource units {max_resource_units}"
        )
    return BoundaryOracleResult(
        case_id="MEM-06",
        invariant_violated=bool(observations),
        observations=tuple(observations),
    )


def skill_admission_integrity(
    candidates: Sequence[SkillAdmissionObservation],
) -> BoundaryOracleResult:
    """Evaluate adapter-native skill admission decisions against declared policy."""
    if not candidates:
        raise ValueError("skill admission candidates must not be empty")

    observations = tuple(
        _admission_mismatch_observation(candidate)
        for candidate in candidates
        if candidate.expected_admitted != candidate.admitted
    )
    return BoundaryOracleResult(
        case_id="SKL-01",
        invariant_violated=bool(observations),
        observations=observations,
    )


def _admission_mismatch_observation(candidate: SkillAdmissionObservation) -> str:
    observation = (
        f"admission mismatch for {candidate.identity}: expected {candidate.expected_admitted}, "
        f"observed {candidate.admitted}"
    )
    return f"{observation}; {candidate.reason}" if candidate.reason else observation


def dependency_resolution_integrity(
    expected_dependencies: Mapping[str, str],
    resolved_dependencies: Mapping[str, str],
) -> BoundaryOracleResult:
    """Evaluate complete adapter-native dependency identities against declared resolution."""
    if not expected_dependencies:
        raise ValueError("expected dependencies must not be empty")
    if not resolved_dependencies:
        raise ValueError("resolved dependencies must not be empty")

    observations = tuple(
        _dependency_observation(
            dependency_id,
            expected_dependencies.get(dependency_id),
            resolved_dependencies.get(dependency_id),
        )
        for dependency_id in sorted(expected_dependencies.keys() | resolved_dependencies.keys())
        if expected_dependencies.get(dependency_id) != resolved_dependencies.get(dependency_id)
    )
    return BoundaryOracleResult(
        case_id="SKL-02",
        invariant_violated=bool(observations),
        observations=observations,
    )


def _dependency_observation(
    dependency_id: str,
    expected_identity: str | None,
    resolved_identity: str | None,
) -> str:
    if expected_identity is None:
        return f"unexpected resolved dependency: {dependency_id} = {resolved_identity}"
    if resolved_identity is None:
        return f"expected dependency not resolved: {dependency_id} = {expected_identity}"
    return (
        f"dependency identity differs for {dependency_id}: "
        f"{resolved_identity} != {expected_identity}"
    )
