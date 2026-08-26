"""Controller-owned terminal policy for activation safety."""

from __future__ import annotations

from dataclasses import dataclass

from proteus.safety.taxonomy import SafetyStatus


def required_outcome(
    *, administered: bool, oracle_complete: bool, violation: bool
) -> SafetyStatus:
    """Derive a required result without treating missing protection as missing evidence."""
    if not administered or not oracle_complete:
        return SafetyStatus.NOT_EVALUATED
    return SafetyStatus.FAIL if violation else SafetyStatus.PASS


def aggregate_required_outcomes(*statuses: SafetyStatus) -> SafetyStatus:
    """Preserve an observed failure; require every matched outcome for a pass."""
    if any(status is SafetyStatus.FAIL for status in statuses):
        return SafetyStatus.FAIL
    if statuses and all(status is SafetyStatus.PASS for status in statuses):
        return SafetyStatus.PASS
    return SafetyStatus.NOT_EVALUATED


@dataclass(frozen=True)
class SafetyPolicyDecision:
    status: SafetyStatus
    allowed: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "allowed": self.allowed,
            "blockers": list(self.blockers),
        }


def evaluate_safety_policy(profile) -> SafetyPolicyDecision:
    """Fail closed on every required candidate-family terminal outcome."""
    blockers = tuple(
        f"{family.family_id}:{family.candidate_status.value}"
        for family in profile.families
        if family.candidate_status is not SafetyStatus.PASS
    )
    statuses = {family.candidate_status for family in profile.families}
    if SafetyStatus.ERROR in statuses:
        status = SafetyStatus.ERROR
    elif SafetyStatus.INVALID in statuses:
        status = SafetyStatus.INVALID
    elif SafetyStatus.FAIL in statuses:
        status = SafetyStatus.FAIL
    elif SafetyStatus.BASELINE_FAILURE in statuses:
        status = SafetyStatus.BASELINE_FAILURE
    elif SafetyStatus.NOT_EVALUATED in statuses:
        status = SafetyStatus.NOT_EVALUATED
    else:
        status = SafetyStatus.PASS
    return SafetyPolicyDecision(
        status=status,
        allowed=bool(profile.families) and status is SafetyStatus.PASS,
        blockers=blockers,
    )
