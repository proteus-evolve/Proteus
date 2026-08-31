"""Controlled sensitivity probes through the active settled-safety runner."""

from __future__ import annotations

import json
from pathlib import Path

from proteus.adapters.minimal import MinimalHarness
from proteus.core.activation import SettledEpisodeSafetyContext
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import (
    CorpusPressurePoint,
    EvidenceCellObservation,
    ProbeEndpoint,
    ProbeObservation,
    ProbeStatuses,
)
from proteus.safety.gate import PostEpisodeSafetyRunner
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS
from proteus.safety.permission_evidence import (
    NativePermissionDecisionValue,
    PermissionCapabilityState,
    PermissionCaseCapability,
    PermissionEvidenceValidity,
)
from proteus.safety.permission_executor import (
    PermissionCaseEvaluation,
    SnapshotPermissionExecutor,
    SnapshotPermissionFamily,
)
from proteus.safety.phase1 import SUITE
from proteus.safety.runtime import RuntimeKind
from proteus.safety.schedule import EveryEpisode, ExplicitEpisodes, SafetySuiteSchedule
from proteus.safety.taxonomy import EvaluationArm, EvidenceStratum, SafetyStatus


def _context(tmp_path: Path, run_id: str, episode: int) -> SettledEpisodeSafetyContext:
    snapshot_root = tmp_path / run_id / f"snapshot-{episode:03d}"
    MinimalHarness().seed(snapshot_root)
    return SettledEpisodeSafetyContext(
        run_id=run_id,
        episode=episode,
        snapshot_ref=SnapshotRef(run_id, episode, SnapshotRole.ACTIVE),
        snapshot_root=snapshot_root,
        trace=(),
        snapshot_commit=f"commit-{episode}",
        episodes_target=3,
    )


def _indicator_path(tmp_path: Path, run_id: str, episode: int) -> Path:
    root = tmp_path / "controller" / "safety" / run_id
    if episode == 0:
        return root / "baseline" / "episode-000" / "indicators.json"
    return root / "episodes" / f"episode-{episode:03d}" / "indicators.json"


def _payload(tmp_path: Path, run_id: str, episode: int) -> dict[str, object]:
    return json.loads(_indicator_path(tmp_path, run_id, episode).read_text(encoding="utf-8"))


def _observation(
    *,
    family_id: str,
    context: SettledEpisodeSafetyContext,
    statuses: ProbeStatuses,
    points: tuple[CorpusPressurePoint, ...] = (),
) -> ProbeObservation:
    return ProbeObservation(
        family_id=family_id,
        snapshot=context.snapshot_ref,
        endpoint=ProbeEndpoint.SETTLED,
        arm=EvaluationArm.FULL_HARNESS,
        runtime_kind=RuntimeKind.DETERMINISTIC,
        status=statuses.module,
        administered=True,
        cells=(
            EvidenceCellObservation(
                cell_id=f"{family_id}.controlled",
                stratum=EvidenceStratum.NATIVE_BOUNDARY,
                status=SafetyStatus.NOT_EVALUATED,
                administered=False,
                oracle_complete=False,
                violation=None,
                evidence_refs=(),
            ),
        ),
        statuses=statuses,
        corpus_pressure_points=points,
        memory_access_mode="exact_key",
    )


class ControlledMemoryRunner(PostEpisodeSafetyRunner):
    def __init__(
        self,
        tmp_path: Path,
        *,
        admission: dict[
            int, SafetyStatus | tuple[SafetyStatus, SafetyStatus]
        ]
        | None = None,
        collapse: dict[
            int, SafetyStatus | tuple[SafetyStatus, SafetyStatus]
        ]
        | None = None,
    ) -> None:
        self._admission_sequence = admission or {}
        self._collapse_sequence = collapse or {}
        adapter = MinimalHarness()
        super().__init__(
            adapter=adapter,
            definitions=SUITE.definitions(),
            controller_root=tmp_path / "controller",
            safety_model="",
            channel_factory=None,
            permission_adapter=adapter.permission_policy_adapter(),
            schedule=SafetySuiteSchedule(
                memory_bad_admission=(
                    EveryEpisode() if admission is not None else ExplicitEpisodes(())
                ),
                memory_collapse=(
                    EveryEpisode() if collapse is not None else ExplicitEpisodes(())
                ),
                tools_permission_drift=ExplicitEpisodes(()),
            ),
        )

    def _fixed_admission_inputs(self, run_id: str):
        del run_id
        return object(), ()

    def _collect_memory(self, **kwargs):
        definition = kwargs["definition"]
        context = kwargs["context"]
        if definition.family_id == "memory_bad_admission":
            value = self._admission_sequence[context.episode]
            status, utility = (
                value if isinstance(value, tuple) else (value, SafetyStatus.PASS)
            )
            return _observation(
                family_id=definition.family_id,
                context=context,
                statuses=ProbeStatuses(
                    module=status,
                    behavior=SafetyStatus.NOT_EVALUATED,
                    utility=utility,
                ),
            )
        value = self._collapse_sequence[context.episode]
        status, utility = (
            value if isinstance(value, tuple) else (value, SafetyStatus.PASS)
        )
        point = _pressure_point(status, utility)
        return _observation(
            family_id=definition.family_id,
            context=context,
            statuses=ProbeStatuses(module=status),
            points=(point,),
        )


def _pressure_point(
    status: SafetyStatus, utility: SafetyStatus = SafetyStatus.PASS
) -> CorpusPressurePoint:
    terminal = status in {SafetyStatus.PASS, SafetyStatus.FAIL}
    retained = 1.0 if status is SafetyStatus.PASS else 0.5 if terminal else None
    return CorpusPressurePoint(
        requested_whitespace_tokens=100,
        actual_whitespace_tokens=100,
        documents_added=1,
        attempted_whitespace_tokens=100,
        documents_attempted=1,
        source_ids=("controlled-document",),
        anchors_present=1,
        anchors_unchanged=1 if status is SafetyStatus.PASS else 0,
        physical_retention=retained,
        exact_access_retention=retained,
        recall_at_1=retained,
        recall_at_3=retained,
        mean_reciprocal_rank=retained,
        pressure_control_passed=(utility is SafetyStatus.PASS) if terminal else None,
        evaluation_status="evaluated" if terminal else "not_evaluated",
        reason="" if terminal else "controlled_gap",
    )


def test_admission_repair_survives_not_evaluated_gap_and_runner_resume(
    tmp_path: Path,
) -> None:
    run_id = "admission-sensitivity"
    sequence = {
        0: SafetyStatus.PASS,
        1: SafetyStatus.FAIL,
        2: SafetyStatus.NOT_EVALUATED,
        3: SafetyStatus.PASS,
    }
    runner = ControlledMemoryRunner(tmp_path, admission=sequence)
    for episode in (0, 1, 2):
        runner.evaluate_settled_episode(_context(tmp_path, run_id, episode))

    resumed = ControlledMemoryRunner(tmp_path, admission=sequence)
    resumed.evaluate_settled_episode(_context(tmp_path, run_id, 3))

    gap = _payload(tmp_path, run_id, 2)["memory_bad_admission"]
    repaired = _payload(tmp_path, run_id, 3)["memory_bad_admission"]
    assert gap["retrieve_delta"]["step_change"] == "unavailable"
    assert repaired["retrieve_delta"] == {
        "baseline_change": "stable_safe",
        "current_episode": 3,
        "previous_episode": 1,
        "step_change": "repair",
    }


def test_admission_utility_change_does_not_move_retrieval_safety(tmp_path: Path) -> None:
    run_id = "admission-utility-sensitivity"
    sequence = {
        0: (SafetyStatus.PASS, SafetyStatus.PASS),
        1: (SafetyStatus.PASS, SafetyStatus.FAIL),
        2: (SafetyStatus.PASS, SafetyStatus.PASS),
    }
    runner = ControlledMemoryRunner(tmp_path, admission=sequence)
    for episode in range(3):
        runner.evaluate_settled_episode(_context(tmp_path, run_id, episode))

    regression = _payload(tmp_path, run_id, 1)["memory_bad_admission"]
    repair = _payload(tmp_path, run_id, 2)["memory_bad_admission"]
    assert regression["retrieve_delta"]["step_change"] == "stable_safe"
    assert regression["original_utility_delta"]["step_change"] == "regression"
    assert repair["retrieve_delta"]["step_change"] == "stable_safe"
    assert repair["original_utility_delta"]["step_change"] == "repair"


def test_collapse_repair_compares_with_last_measured_component(tmp_path: Path) -> None:
    run_id = "collapse-sensitivity"
    sequence = {
        0: SafetyStatus.PASS,
        1: SafetyStatus.FAIL,
        2: SafetyStatus.NOT_EVALUATED,
        3: SafetyStatus.PASS,
    }
    runner = ControlledMemoryRunner(tmp_path, collapse=sequence)
    for episode in range(4):
        runner.evaluate_settled_episode(_context(tmp_path, run_id, episode))

    repaired = _payload(tmp_path, run_id, 3)["memory_collapse"]
    assert repaired["physical_delta"]["previous_episode"] == 1
    assert repaired["physical_delta"]["step_change"] == "repair"
    assert repaired["exact_access_delta"]["step_change"] == "repair"
    assert repaired["utility_delta"]["step_change"] == "stable_safe"


def test_collapse_utility_change_does_not_move_retention_safety(tmp_path: Path) -> None:
    run_id = "collapse-utility-sensitivity"
    sequence = {
        0: (SafetyStatus.PASS, SafetyStatus.PASS),
        1: (SafetyStatus.PASS, SafetyStatus.FAIL),
        2: (SafetyStatus.PASS, SafetyStatus.PASS),
    }
    runner = ControlledMemoryRunner(tmp_path, collapse=sequence)
    for episode in range(3):
        runner.evaluate_settled_episode(_context(tmp_path, run_id, episode))

    regression = _payload(tmp_path, run_id, 1)["memory_collapse"]
    repair = _payload(tmp_path, run_id, 2)["memory_collapse"]
    assert regression["physical_delta"]["step_change"] == "stable_safe"
    assert regression["utility_delta"]["step_change"] == "regression"
    assert repair["physical_delta"]["step_change"] == "stable_safe"
    assert repair["utility_delta"]["step_change"] == "repair"


class ControlledPermissionExecutor(SnapshotPermissionExecutor):
    def __init__(
        self,
        sequence: dict[
            int,
            tuple[
                str | None,
                bool | None,
                str | None,
                bool | None,
            ],
        ],
    ) -> None:
        self._sequence = sequence

    def execute(self, request):
        protected_value, protected_effect, allowed_value, allowed_effect = (
            self._sequence[request.source.snapshot.episode]
        )
        case_spec = next(
            case for case in PERMISSION_CASE_SPECS
            if case.case_id == "protected_overwrite"
        )
        protected_decision = (
            NativePermissionDecisionValue(protected_value)
            if protected_value is not None
            else None
        )
        allowed_decision = (
            NativePermissionDecisionValue(allowed_value)
            if allowed_value is not None
            else None
        )
        complete = all(
            value is not None
            for value in (
                protected_decision,
                protected_effect,
                allowed_decision,
                allowed_effect,
            )
        )
        evaluation = PermissionCaseEvaluation(
            case_id=case_spec.case_id,
            case_spec=case_spec,
            snapshot=request.source.snapshot,
            capability=PermissionCaseCapability(
                PermissionCapabilityState.SUPPORTED,
                "controlled-native-policy",
                "",
            ),
            protected=None,
            allowed=None,
            protected_callable_inventory=None,
            allowed_callable_inventory=None,
            protected_proposed=True if complete else None,
            protected_attempted=True if complete else None,
            protected_decision=protected_decision,
            protected_effect_committed=protected_effect,
            allowed_proposed=True if complete else None,
            allowed_attempted=True if complete else None,
            allowed_decision=allowed_decision,
            allowed_effect_committed=allowed_effect,
            validity=PermissionEvidenceValidity.VALID,
            reasons=() if complete else ("controlled_gap",),
            evidence_refs=(),
        )
        return SnapshotPermissionFamily(
            snapshot=request.source.snapshot,
            cases=(evaluation,),
            validity=PermissionEvidenceValidity.VALID,
        )


def _permission_runner(
    tmp_path: Path,
    sequence: dict[
        int,
        tuple[str | None, bool | None, str | None, bool | None],
    ],
) -> PostEpisodeSafetyRunner:
    adapter = MinimalHarness()
    return PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=None,
        permission_adapter=adapter.permission_policy_adapter(),
        permission_executor=ControlledPermissionExecutor(sequence),
        schedule=SafetySuiteSchedule(
            memory_bad_admission=ExplicitEpisodes(()),
            memory_collapse=ExplicitEpisodes(()),
            tools_permission_drift=EveryEpisode(),
        ),
    )


def test_permission_decision_regression_and_repair_survive_gap_and_resume(
    tmp_path: Path,
) -> None:
    run_id = "permission-sensitivity"
    sequence = {
        0: ("deny", False, "allow", True),
        1: (None, None, None, None),
        2: ("allow", False, "allow", True),
        3: ("deny", False, "allow", True),
    }
    runner = _permission_runner(tmp_path, sequence)
    for episode in (0, 1, 2):
        runner.evaluate_settled_episode(_context(tmp_path, run_id, episode))

    regression = _payload(tmp_path, run_id, 2)["tools_permission_drift"]["cases"][0]
    assert regression["change_from_previous"]["protected"] == "regression"
    assert regression["change_from_baseline"]["protected"] == "regression"
    assert regression["display"].startswith("Protection regression")

    resumed = _permission_runner(tmp_path, sequence)
    resumed.evaluate_settled_episode(_context(tmp_path, run_id, 3))
    repair = _payload(tmp_path, run_id, 3)["tools_permission_drift"]["cases"][0]
    assert repair["change_from_previous"]["protected"] == "repair"
    assert repair["change_from_baseline"]["protected"] == "stable_safe"
    assert repair["display"].startswith("Protection repair")


def test_permission_allowed_decision_has_utility_regression_and_repair(
    tmp_path: Path,
) -> None:
    run_id = "permission-utility-sensitivity"
    sequence = {
        0: ("deny", False, "allow", True),
        1: ("deny", False, "deny", True),
        2: ("deny", False, "allow", True),
    }
    runner = _permission_runner(tmp_path, sequence)
    for episode in range(3):
        runner.evaluate_settled_episode(_context(tmp_path, run_id, episode))

    regression = _payload(tmp_path, run_id, 1)["tools_permission_drift"]["cases"][0]
    repair = _payload(tmp_path, run_id, 2)["tools_permission_drift"]["cases"][0]
    assert regression["change_from_previous"] == {
        "allowed": "utility_regression",
        "protected": "stable_safe",
    }
    assert regression["display"] == "Utility regression — allowed control denied"
    assert repair["change_from_previous"] == {
        "allowed": "utility_repair",
        "protected": "stable_safe",
    }
