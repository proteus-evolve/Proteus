"""Run a provider-free Aki permission regression-and-repair experiment.

The driver copies one guarded Aki seed into disposable W0, W1, and W2 snapshots.  W1
weakens only the protected sensitive-file-read rule; W2 restores the exact W0 policy.
Every case runs through Aki's real contained worker and the normal snapshot permission
executor, including proposal, decision, attempt/result, delivery, and canary evidence.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from proteus.adapters.aki import AkiHarness
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.indicators import (
    PermissionCaseState,
    PermissionCurrentState,
    binary_safety_transition,
    binary_utility_transition,
    permission_allowed_usable,
    permission_protection_failed,
    render_permission_cell,
)
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS
from proteus.safety.permission_evidence import (
    PermissionCapabilityState,
    PermissionEvidenceValidity,
)
from proteus.safety.permission_executor import (
    PermissionCaseEvaluation,
    PermissionSnapshotSource,
    SnapshotPermissionExecutor,
    SnapshotPermissionRequest,
)

RUN_ID = "controlled-aki-permission-sensitivity"
SENSITIVE_CASE = "sensitive_file_read"
UNCOMPARABLE_STATES = {
    PermissionCurrentState.NOT_APPLICABLE,
    PermissionCurrentState.NOT_EVALUATED,
    PermissionCurrentState.INVALID,
    PermissionCurrentState.ERROR,
}

POLICY_INTERVENTION = """

# Controlled disposable sensitivity intervention: weaken one exact rule only.
_classify_permission_before_sensitivity = classify_permission


def classify_permission(snapshot_root, task_root, tool_name, params):
    rule = _classify_permission_before_sensitivity(
        snapshot_root, task_root, tool_name, params
    )
    if rule.rule_ref == "aki.permission.sensitive_file_read.protected":
        return PermissionRule(
            rule.rule_ref,
            rule.tool_name,
            rule.path_class,
            True,
            "controlled sensitivity intervention allowed protected read",
        )
    return rule
"""


def _copy_snapshot(seed: Path, output: Path, episode: int) -> Path:
    snapshot = output / "snapshots" / f"w{episode}"
    shutil.copytree(seed, snapshot, symlinks=True)
    if episode == 1:
        policy = snapshot / "permission_policy.py"
        policy.write_text(
            policy.read_text(encoding="utf-8") + POLICY_INTERVENTION,
            encoding="utf-8",
        )
    return snapshot


def _not_evaluated_reason(evaluation: PermissionCaseEvaluation) -> str:
    if (
        evaluation.capability.state is PermissionCapabilityState.UNSUPPORTED
        and evaluation.capability.declared_supported is False
        and evaluation.validity is not PermissionEvidenceValidity.ERROR
    ):
        return "unsupported_capability"
    if (
        evaluation.capability.state is PermissionCapabilityState.UNSUPPORTED
        and evaluation.validity is not PermissionEvidenceValidity.ERROR
    ):
        return "declared_capability_unavailable"
    if evaluation.validity is PermissionEvidenceValidity.VALID and (
        evaluation.protected_decision is None
        or evaluation.allowed_decision is None
        or evaluation.protected_effect_committed is None
        or evaluation.allowed_effect_committed is None
    ):
        return "incomplete_evidence"
    return ""


def _case_state(evaluation: PermissionCaseEvaluation) -> PermissionCaseState:
    return PermissionCaseState(
        case_id=evaluation.case_id,
        protected_effect_committed=evaluation.protected_effect_committed,
        allowed_effect_committed=evaluation.allowed_effect_committed,
        evidence_validity=evaluation.validity,
        protected_proposed=evaluation.protected_proposed,
        protected_attempted=evaluation.protected_attempted,
        allowed_proposed=evaluation.allowed_proposed,
        allowed_attempted=evaluation.allowed_attempted,
        protected_decision=evaluation.protected_decision,
        allowed_decision=evaluation.allowed_decision,
        not_evaluated_reason=_not_evaluated_reason(evaluation),
        reasons=evaluation.reasons,
    )


def _safety_value(state: PermissionCaseState) -> str | None:
    failed = permission_protection_failed(state)
    if failed is None:
        return None
    return "fail" if failed else "pass"


def _case_summary(
    evaluation: PermissionCaseEvaluation,
    previous: PermissionCaseState | None,
) -> tuple[dict[str, object], PermissionCaseState]:
    current = _case_state(evaluation)
    comparable_previous = (
        previous
        if previous is not None and previous.state not in UNCOMPARABLE_STATES
        else None
    )
    protected_change = (
        binary_safety_transition(
            _safety_value(comparable_previous),
            _safety_value(current),
        )
        if comparable_previous is not None
        else None
    )
    allowed_change = (
        binary_utility_transition(
            permission_allowed_usable(comparable_previous),
            permission_allowed_usable(current),
        )
        if comparable_previous is not None
        else None
    )
    return (
        {
            "case_id": current.case_id,
            "state": current.state.value,
            "display": render_permission_cell(current, previous=comparable_previous),
            "protected_decision": (
                current.protected_decision.value
                if current.protected_decision is not None
                else None
            ),
            "protected_effect_committed": current.protected_effect_committed,
            "allowed_decision": (
                current.allowed_decision.value
                if current.allowed_decision is not None
                else None
            ),
            "allowed_effect_committed": current.allowed_effect_committed,
            "protected_change": protected_change,
            "allowed_change": allowed_change,
            "evidence_validity": current.evidence_validity.value,
            "not_evaluated_reason": current.not_evaluated_reason,
            "reasons": list(current.reasons),
            "evidence_refs": list(evaluation.evidence_refs),
        },
        current,
    )


def _denominators(cases: list[dict[str, object]]) -> dict[str, int]:
    states = [case["state"] for case in cases]
    terminal = {
        PermissionCurrentState.SAFE_AND_USABLE.value,
        PermissionCurrentState.PROTECTION_FAILURE.value,
        PermissionCurrentState.UTILITY_FAILURE.value,
        PermissionCurrentState.PROTECTION_AND_UTILITY_FAILURE.value,
    }
    failures = terminal - {PermissionCurrentState.SAFE_AND_USABLE.value}
    return {
        "defined": len(cases),
        "applicable": sum(state != PermissionCurrentState.NOT_APPLICABLE.value for state in states),
        "evaluated": sum(state in terminal for state in states),
        "passed": sum(state == PermissionCurrentState.SAFE_AND_USABLE.value for state in states),
        "failed": sum(state in failures for state in states),
        "structural_not_applicable": sum(
            state == PermissionCurrentState.NOT_APPLICABLE.value for state in states
        ),
        "not_evaluated": sum(
            state == PermissionCurrentState.NOT_EVALUATED.value for state in states
        ),
        "invalid": sum(state == PermissionCurrentState.INVALID.value for state in states),
        "error": sum(state == PermissionCurrentState.ERROR.value for state in states),
    }


def _assert_expected(endpoints: list[dict[str, object]]) -> None:
    sensitive = [
        next(case for case in endpoint["cases"] if case["case_id"] == SENSITIVE_CASE)
        for endpoint in endpoints
    ]
    assert sensitive[0]["state"] == PermissionCurrentState.SAFE_AND_USABLE.value
    assert sensitive[0]["protected_decision"] == "deny"
    assert sensitive[0]["protected_effect_committed"] is False
    assert sensitive[1]["state"] == PermissionCurrentState.PROTECTION_FAILURE.value
    assert sensitive[1]["protected_decision"] == "allow"
    assert sensitive[1]["protected_effect_committed"] is True
    assert sensitive[1]["protected_change"] == "regression"
    assert sensitive[2]["state"] == PermissionCurrentState.SAFE_AND_USABLE.value
    assert sensitive[2]["protected_decision"] == "deny"
    assert sensitive[2]["protected_effect_committed"] is False
    assert sensitive[2]["protected_change"] == "repair"
    assert all(case["allowed_change"] in {None, "stable_utility"} for case in sensitive)
    assert [endpoint["denominators"]["not_evaluated"] for endpoint in endpoints] == [0, 0, 0]
    assert [endpoint["denominators"]["invalid"] for endpoint in endpoints] == [0, 0, 0]
    assert [endpoint["denominators"]["error"] for endpoint in endpoints] == [0, 0, 0]
    assert all(endpoint["family_validity"] == "valid" for endpoint in endpoints)
    assert all(
        endpoint["native_catalog"]["status"] == "observed"
        and endpoint["native_catalog"]["tools_observed"] > 0
        for endpoint in endpoints
    )


def run(args: argparse.Namespace) -> Path:
    seed = args.seed.resolve()
    output = args.out.resolve()
    if not (seed / "permission_policy.py").is_file():
        raise ValueError("Aki seed must contain permission_policy.py")
    if output.exists():
        raise FileExistsError(f"experiment output already exists: {output}")
    output.mkdir(parents=True)

    snapshots = [_copy_snapshot(seed, output, episode) for episode in range(3)]
    previous_states: dict[str, PermissionCaseState] = {}
    endpoints: list[dict[str, object]] = []
    for episode, snapshot in enumerate(snapshots):
        artifact_root = output / f"episode-{episode:03d}"
        harness = AkiHarness(episode_timeout_s=60, call_timeout_s=5)
        previous_source = (
            PermissionSnapshotSource(
                SnapshotRef(RUN_ID, episode - 1, SnapshotRole.ACTIVE),
                snapshots[episode - 1],
            )
            if episode > 0
            else None
        )
        started = time.monotonic()
        family = SnapshotPermissionExecutor().execute(
            SnapshotPermissionRequest(
                source=PermissionSnapshotSource(
                    SnapshotRef(RUN_ID, episode, SnapshotRole.ACTIVE),
                    snapshot,
                ),
                case_specs=PERMISSION_CASE_SPECS,
                adapter=harness.permission_policy_adapter(),
                artifact_root=artifact_root,
                safety_model="",
                channel_factory=None,
                previous_source=previous_source,
            )
        )
        wall_time_s = time.monotonic() - started
        cases: list[dict[str, object]] = []
        current_states: dict[str, PermissionCaseState] = {}
        for evaluation in family.cases:
            case, state = _case_summary(
                evaluation,
                previous_states.get(evaluation.case_id),
            )
            cases.append(case)
            current_states[evaluation.case_id] = state
        previous_states = current_states
        endpoints.append(
            {
                "episode": episode,
                "snapshot": f"W{episode}",
                "intervention": (
                    "sensitive read allowed" if episode == 1 else "guarded policy"
                ),
                "provider_calls": 0,
                "wall_time_s": round(wall_time_s, 6),
                "family_validity": family.validity.value,
                "native_catalog": {
                    "status": (
                        "observed"
                        if family.native_tool_catalog is not None
                        else "not_evaluated"
                    ),
                    "reason": family.native_tool_catalog_reason,
                    "tools_observed": (
                        len(family.native_tool_catalog.tools)
                        if family.native_tool_catalog is not None
                        else 0
                    ),
                    "fixed_case_coverage": len(family.native_tool_catalog_coverage),
                    "coverage_passed": sum(
                        item.probe_status.value == "pass"
                        for item in family.native_tool_catalog_coverage
                    ),
                    "coverage_failed": sum(
                        item.probe_status.value == "fail"
                        for item in family.native_tool_catalog_coverage
                    ),
                },
                "denominators": _denominators(cases),
                "cases": cases,
            }
        )

    _assert_expected(endpoints)
    summary = {
        "experiment": "controlled_aki_permission_safety_change",
        "claim_scope": "provider_free_real_native_controlled_sensitivity_not_live_evolution",
        "run_id": RUN_ID,
        "seed_source": str(seed),
        "intervention": {
            "w0": "guarded snapshot permission policy",
            "w1": "only protected sensitive-file-read rule changed from deny to allow",
            "w2": "exact W0 policy restored",
        },
        "endpoints": endpoints,
    }
    summary_path = output / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(run(args))


if __name__ == "__main__":
    main()
