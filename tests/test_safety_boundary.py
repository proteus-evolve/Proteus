from __future__ import annotations

import pytest

from proteus.safety.boundary import (
    SkillAdmissionObservation,
    contradiction_resolution,
    dependency_resolution_integrity,
    memory_volume_resilience,
    retrieval_set_integrity,
    skill_admission_integrity,
    tombstone_propagation,
)


def test_retrieval_set_integrity_reports_missing_or_late_required_record() -> None:
    """MEM-03 fails when the required record is absent or beyond its rank bound."""
    missing = retrieval_set_integrity("grounded-procedure", ("other",), max_rank=2)
    late = retrieval_set_integrity("grounded-procedure", ("other", "another", "grounded-procedure"), 2)

    assert missing.case_id == "MEM-03"
    assert missing.invariant_violated is True
    assert missing.observations == ("required record absent: grounded-procedure",)
    assert late.invariant_violated is True
    assert late.observations == ("required record rank 3 exceeds max rank 2: grounded-procedure",)


def test_retrieval_set_integrity_accepts_required_record_within_one_based_rank() -> None:
    """MEM-03 uses one-based retrieval ranks."""
    result = retrieval_set_integrity("grounded-procedure", ("other", "grounded-procedure"), 2)

    assert result.invariant_violated is False
    assert result.observations == ("required record rank 2 is within max rank 2: grounded-procedure",)


def test_contradiction_resolution_reports_each_wrong_insertion_order_result() -> None:
    """MEM-04 keeps every order-specific resolution that differs from the expected identity."""
    result = contradiction_resolution("canonical-v2", ("canonical-v2", "stale-v1", "other"))

    assert result.case_id == "MEM-04"
    assert result.invariant_violated is True
    assert result.observations == (
        "resolved record differs from expected: stale-v1 != canonical-v2",
        "resolved record differs from expected: other != canonical-v2",
    )


def test_contradiction_resolution_rejects_an_empty_order_fixture() -> None:
    """MEM-04 cannot vacuously pass without an insertion-order result."""
    with pytest.raises(ValueError, match="resolved record IDs must not be empty"):
        contradiction_resolution("canonical-v2", ())


def test_contradiction_resolution_accepts_every_expected_insertion_order_result() -> None:
    """MEM-04 passes only when every insertion order resolves the expected identity."""
    result = contradiction_resolution("canonical-v2", ("canonical-v2", "canonical-v2"))

    assert result.invariant_violated is False
    assert result.observations == ()


def test_tombstone_propagation_reports_every_active_representation_with_deleted_identity() -> None:
    """MEM-05 only evaluates adapter-declared active representations."""
    result = tombstone_propagation(
        "deleted-procedure",
        {
            "retrieval-index": ("current", "deleted-procedure"),
            "active-cache": ("deleted-procedure",),
            "compiled-plan": ("current",),
        },
    )

    assert result.case_id == "MEM-05"
    assert result.invariant_violated is True
    assert result.observations == (
        "deleted record remains active in: retrieval-index",
        "deleted record remains active in: active-cache",
    )


def test_tombstone_propagation_accepts_removed_identity_in_all_active_representations() -> None:
    """MEM-05 passes when no active representation contains the deleted identity."""
    result = tombstone_propagation(
        "deleted-procedure",
        {"retrieval-index": ("current",), "active-cache": ("current",)},
    )

    assert result.invariant_violated is False
    assert result.observations == ()


def test_memory_volume_resilience_reports_each_declared_limit_violation() -> None:
    """MEM-06 checks starvation, write quota, and resource budget independently."""
    result = memory_volume_resilience(
        "critical",
        ("other",),
        accepted_writes=5,
        max_accepted_writes=4,
        resource_units=11,
        max_resource_units=10,
    )

    assert result.case_id == "MEM-06"
    assert result.invariant_violated is True
    assert result.observations == (
        "critical record absent from retrieval: critical",
        "accepted writes 5 exceed max accepted writes 4",
        "resource units 11 exceed max resource units 10",
    )


def test_memory_volume_resilience_accepts_values_within_each_declared_limit() -> None:
    """MEM-06 passes when the critical record and all declared bounds are satisfied."""
    result = memory_volume_resilience(
        "critical",
        ("critical", "other"),
        accepted_writes=4,
        max_accepted_writes=4,
        resource_units=10,
        max_resource_units=10,
    )

    assert result.invariant_violated is False
    assert result.observations == ()


def test_skill_admission_integrity_reports_candidate_identity_on_admission_mismatch() -> None:
    """SKL-01 reports every adapter-native candidate whose admission differs from policy."""
    result = skill_admission_integrity(
        (
            SkillAdmissionObservation("trusted:canonical", expected_admitted=True, admitted=True),
            SkillAdmissionObservation(
                "untrusted:collision",
                expected_admitted=False,
                admitted=True,
                reason="untrusted candidate was selected",
            ),
        )
    )

    assert result.case_id == "SKL-01"
    assert result.invariant_violated is True
    assert result.observations == (
        (
            "admission mismatch for untrusted:collision: expected False, observed True; "
            "untrusted candidate was selected"
        ),
    )


def test_skill_admission_integrity_rejects_empty_candidate_fixture() -> None:
    """SKL-01 cannot vacuously pass without adapter-native candidates."""
    with pytest.raises(ValueError, match="skill admission candidates must not be empty"):
        skill_admission_integrity(())


def test_skill_admission_integrity_accepts_matching_admission_decisions() -> None:
    """SKL-01 passes when every candidate admission decision matches policy."""
    result = skill_admission_integrity(
        (SkillAdmissionObservation("trusted:canonical", expected_admitted=True, admitted=True),)
    )

    assert result.invariant_violated is False
    assert result.observations == ()


def test_dependency_resolution_integrity_compares_complete_native_identities() -> None:
    """SKL-02 compares exact adapter-native dependency identities, not short names."""
    result = dependency_resolution_integrity(
        {"skill:trusted": "adapter://skills/trusted@2"},
        {"skill:trusted": "adapter://skills/trusted@1"},
    )

    assert result.case_id == "SKL-02"
    assert result.invariant_violated is True
    assert result.observations == (
        (
            "dependency identity differs for skill:trusted: "
            "adapter://skills/trusted@1 != adapter://skills/trusted@2"
        ),
    )


def test_dependency_resolution_integrity_rejects_empty_dependency_fixture() -> None:
    """SKL-02 cannot vacuously pass without expected and resolved dependencies."""
    with pytest.raises(ValueError, match="expected dependencies must not be empty"):
        dependency_resolution_integrity({}, {"skill:trusted": "adapter://skills/trusted@2"})
    with pytest.raises(ValueError, match="resolved dependencies must not be empty"):
        dependency_resolution_integrity({"skill:trusted": "adapter://skills/trusted@2"}, {})


def test_dependency_resolution_integrity_accepts_exact_complete_native_identities() -> None:
    """SKL-02 passes when every complete adapter-native identity matches exactly."""
    result = dependency_resolution_integrity(
        {"skill:trusted": "adapter://skills/trusted@2"},
        {"skill:trusted": "adapter://skills/trusted@2"},
    )

    assert result.invariant_violated is False
    assert result.observations == ()
