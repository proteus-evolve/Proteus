from __future__ import annotations

import dataclasses
import typing

import pytest

from proteus.safety import permission_cases, tools_permission_drift
from proteus.safety.permission_cases import PermissionOperationSpec
from proteus.safety.permission_evidence import (
    NativePermissionBinding,
    PermissionCapabilityState,
    PermissionCaseCapability,
)
from proteus.safety.phase1 import SUITE, phase1_case_families


def test_permission_family_v2_reuses_one_exact_ordered_catalog() -> None:
    phase1 = next(
        item
        for item in phase1_case_families()
        if item.family_id == "tools_permission_drift"
    )
    isolated = tools_permission_drift.SUITE.definitions()

    assert SUITE.version == "2"
    assert phase1.family_version == "2"
    assert isolated == (phase1,)
    assert isolated[0] is phase1
    assert tuple(case.case_id for case in phase1.permission_cases) == (
        "recursive_deletion",
        "protected_overwrite",
        "policy_mutation",
        "tool_skill_capability_minting",
        "workspace_boundary",
        "command_execution",
    )
    assert all(
        case.required_native_chain
        == ("proposal", "decision", "attempt_result", "delivery", "canary")
        for case in phase1.permission_cases
    )


def test_permission_types_reject_controller_decisions_and_incomplete_capabilities() -> None:
    with pytest.raises(ValueError, match="supported capability requires a native mechanism"):
        PermissionCaseCapability(PermissionCapabilityState.SUPPORTED, "", "")
    with pytest.raises(
        ValueError,
        match="unsupported capability requires a missing requirement",
    ):
        PermissionCaseCapability(PermissionCapabilityState.UNSUPPORTED, "", "")
    assert "externally_authorized" not in {
        field.name for field in dataclasses.fields(PermissionOperationSpec)
    }
    assert "expected_decision" not in {
        field.name for field in dataclasses.fields(PermissionOperationSpec)
    }
    assert {field.name for field in dataclasses.fields(NativePermissionBinding)}.isdisjoint(
        {"deny", "deny_callback", "policy_mode", "expected_outcome"}
    )


def test_version1_synthetic_permission_api_is_absent_from_current_code() -> None:
    from proteus import safety
    from proteus.adapters import aki_container_worker
    from proteus.adapters.aki_live_worker import AkiWorkerPlan, BoundaryRecord
    from proteus.adapters.aki_safety import AkiSafetyRuntime
    from proteus.safety import evidence, phase1_runtime, runtime

    assert not hasattr(runtime, "EffectRequest")
    assert not hasattr(evidence, "PermissionObservation")
    assert not hasattr(phase1_runtime, "run_tools_permission_drift")
    assert "tools_permission_drift" not in phase1_runtime.PHASE1_EXECUTORS
    assert not hasattr(safety, "EffectRequest")
    assert not hasattr(safety, "PermissionObservation")
    assert not hasattr(safety, "run_tools_permission_drift")
    assert not hasattr(runtime.HarnessSafetyRuntime, "invoke_effect")
    assert not hasattr(runtime.HarnessSafetyRuntime, "effect_oracle")
    assert "effect_contracts" not in {
        field.name for field in dataclasses.fields(AkiWorkerPlan)
    }
    assert {
        field.name for field in dataclasses.fields(BoundaryRecord)
    }.isdisjoint({"effect_id", "external_effect_committed"})
    assert not hasattr(aki_container_worker, "_effect_contract")
    assert "effect_contracts" not in AkiSafetyRuntime._container_payload(
        AkiWorkerPlan(episode=1)
    )
    assert not hasattr(permission_cases, "operation")
    assert not hasattr(permission_cases, "case")
    assert typing.get_type_hints(permission_cases)["PERMISSION_CASE_SPECS"] == tuple[
        permission_cases.PermissionPolicyCaseSpec, ...
    ]
