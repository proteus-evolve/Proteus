"""Orchestration contracts for post-episode settled safety."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from proteus.adapters.minimal import MinimalHarness, mock_policy
from proteus.core.activation import SettledEpisodeSafetyContext
from proteus.core.disposition import review
from proteus.core.episode import RunConfig, run
from proteus.core.goal import GoalConfig
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import ProbeEndpoint
from proteus.safety.gate import PostEpisodeSafetyRunner
from proteus.safety.indicators import (
    FamilyExecution,
    MemoryBadAdmissionIndicators,
    MemoryCollapseIndicators,
    PermissionCaseIndicators,
    PermissionCaseState,
    PermissionChange,
    ToolsPermissionIndicators,
)
from proteus.safety.permission_evidence import (
    NativePermissionDecisionValue,
    PermissionEvidenceValidity,
)
from proteus.safety.phase1 import SUITE
from proteus.safety.schedule import (
    EveryEpisode,
    EveryN,
    ExplicitEpisodes,
    SafetySuiteSchedule,
)
from proteus.safety.taxonomy import SafetyStatus


@dataclass
class FakeFamily:
    family_id: str
    schedule: object
    calls: list = field(default_factory=list)
    fail: bool = False

    def evaluate(self, context: SettledEpisodeSafetyContext):
        self.calls.append(context)
        if context.snapshot_ref.role is not SnapshotRole.ACTIVE:
            raise AssertionError("settled safety must observe the running snapshot")
        if getattr(context, "endpoint", None) in {
            ProbeEndpoint.ACTIVE,
            ProbeEndpoint.CANDIDATE,
        }:
            raise AssertionError("ACTIVE/CANDIDATE endpoint evaluation is forbidden")
        if self.fail:
            raise RuntimeError(f"{self.family_id} boom")
        if self.family_id == "memory_bad_admission":
            return MemoryBadAdmissionIndicators(
                execution=FamilyExecution("evaluated", context.episode, ()),
                retrieve_status=SafetyStatus.PASS,
                follow_status=SafetyStatus.PASS,
                original_utility_status=SafetyStatus.PASS,
                unsafe_retrievable=False,
                unsafe_followed=False,
                original_still_present=True,
            )
        if self.family_id == "memory_collapse":
            return MemoryCollapseIndicators(
                execution=FamilyExecution("evaluated", context.episode, ()),
                physical_retention_status=SafetyStatus.PASS,
                retrieval_retention_status=SafetyStatus.PASS,
                pressure_utility_status=SafetyStatus.PASS,
            )
        return ToolsPermissionIndicators(
            execution=FamilyExecution("evaluated", context.episode, ()),
            cases=(
                PermissionCaseIndicators(
                    current=PermissionCaseState(
                        "protected_overwrite",
                        False,
                        True,
                        PermissionEvidenceValidity.VALID,
                        protected_decision=NativePermissionDecisionValue.DENY,
                        allowed_decision=NativePermissionDecisionValue.ALLOW,
                    ),
                    change_from_previous=None,
                    change_from_baseline=None,
                ),
            ),
        )


class RecordingRunner(PostEpisodeSafetyRunner):
    def __init__(self, tmp_path: Path, families, schedule, episodes_target: int) -> None:
        super().__init__(
            adapter=MinimalHarness(),
            definitions=SUITE.definitions(),
            controller_root=tmp_path / "controller",
            safety_model="",
            channel_factory=None,
            schedule=schedule,
            episodes_target=episodes_target,
            families=families,
        )
        self.stage_calls: list[SettledEpisodeSafetyContext] = []

    def evaluate_settled_episode(self, context: SettledEpisodeSafetyContext):
        self.stage_calls.append(context)
        return super().evaluate_settled_episode(context)


def _schedule(*, collapse=None, admission=None, permission=None):
    return SafetySuiteSchedule(
        memory_bad_admission=admission if admission is not None else EveryEpisode(),
        memory_collapse=collapse if collapse is not None else EveryN(5),
        tools_permission_drift=permission if permission is not None else EveryEpisode(),
    )


def test_no_family_runs_before_settlement(tmp_path: Path) -> None:
    from proteus.core.episode import eval_history_path

    class GuardFamily(FakeFamily):
        def evaluate(self, context: SettledEpisodeSafetyContext):
            if context.episode < 1:
                return super().evaluate(context)
            history = eval_history_path(tmp_path / "run")
            assert history.is_file(), "safety must not run before durable history write"
            rows = __import__("json").loads(history.read_text(encoding="utf-8"))
            assert any(row.get("episode") == context.episode for row in rows)
            return super().evaluate(context)

    families = [
        GuardFamily("memory_bad_admission", EveryEpisode()),
        GuardFamily("memory_collapse", EveryN(5)),
        GuardFamily("tools_permission_drift", EveryEpisode()),
    ]
    runner = RecordingRunner(tmp_path, families, _schedule(), episodes_target=2)
    run(
        RunConfig(
            name="settled-first",
            run_id="run-settled",
            adapter=MinimalHarness(policy=mock_policy),
            disposition=review("notes"),
            goal=GoalConfig.no_goal(),
            root=tmp_path / "run",
            model="mock",
            episodes=2,
            seed=0,
            safety_runner=runner,
        )
    )
    assert [ctx.episode for ctx in families[0].calls if ctx.episode >= 1] == [1, 2]


def test_selected_family_runs_only_on_configured_episodes(tmp_path: Path) -> None:
    collapse = FakeFamily("memory_collapse", ExplicitEpisodes({2, 4}))
    admission = FakeFamily("memory_bad_admission", EveryEpisode())
    permission = FakeFamily("tools_permission_drift", EveryEpisode())
    schedule = _schedule(collapse=ExplicitEpisodes({2, 4}))
    runner = RecordingRunner(
        tmp_path, [admission, collapse, permission], schedule, episodes_target=4
    )
    for episode in range(5):
        _evaluate(runner, tmp_path, episode, episodes_target=4)
    assert [c.episode for c in collapse.calls] == [0, 2, 4]


def test_every_n_includes_first_settled_episode_and_multiples(tmp_path: Path) -> None:
    collapse = FakeFamily("memory_collapse", EveryN(5))
    admission = FakeFamily("memory_bad_admission", EveryEpisode())
    permission = FakeFamily("tools_permission_drift", EveryEpisode())
    runner = RecordingRunner(
        tmp_path, [admission, collapse, permission], _schedule(), episodes_target=10
    )
    statuses = {}
    for episode in range(11):
        _evaluate(runner, tmp_path, episode, episodes_target=10)
        if episode == 0:
            path = (
                tmp_path
                / "controller"
                / "safety"
                / "run-post"
                / "baseline"
                / "episode-000"
                / "indicators.json"
            )
        else:
            path = (
                tmp_path
                / "controller"
                / "safety"
                / "run-post"
                / "episodes"
                / f"episode-{episode:03d}"
                / "indicators.json"
            )
        payload = __import__("json").loads(path.read_text(encoding="utf-8"))
        statuses[episode] = payload["memory_collapse"]["execution"]["schedule_status"]
    assert statuses[0] == "evaluated"
    assert statuses[1] == "evaluated"
    assert statuses[5] == "evaluated"
    assert statuses[10] == "evaluated"
    assert [c.episode for c in collapse.calls] == [0, 1, 5, 10]
    for episode in (2, 3, 4, 6, 7, 8, 9):
        assert statuses[episode] == "not_scheduled"


def test_deltas_use_stored_records_without_rerunning(tmp_path: Path) -> None:
    class CountingPermission(FakeFamily):
        def evaluate(self, context: SettledEpisodeSafetyContext):
            result = super().evaluate(context)
            previous = None if context.episode == 0 else PermissionChange(
                protected="stable_safe", allowed="stable_utility"
            )
            cases = (
                PermissionCaseIndicators(
                    current=PermissionCaseState(
                        "protected_overwrite",
                        False,
                        True,
                        PermissionEvidenceValidity.VALID,
                        protected_decision=NativePermissionDecisionValue.DENY,
                        allowed_decision=NativePermissionDecisionValue.ALLOW,
                    ),
                    change_from_previous=previous,
                    change_from_baseline=previous,
                ),
            )
            return ToolsPermissionIndicators(execution=result.execution, cases=cases)

    permission = CountingPermission("tools_permission_drift", EveryEpisode())
    runner = RecordingRunner(
        tmp_path,
        [
            FakeFamily("memory_bad_admission", EveryEpisode()),
            FakeFamily("memory_collapse", EveryN(5)),
            permission,
        ],
        _schedule(),
        episodes_target=2,
    )
    _evaluate(runner, tmp_path, 0, episodes_target=2)
    _evaluate(runner, tmp_path, 1, episodes_target=2)
    _evaluate(runner, tmp_path, 2, episodes_target=2)
    assert [c.episode for c in permission.calls] == [0, 1, 2]
    episode_payload = __import__("json").loads(
        (
            tmp_path
            / "controller"
            / "safety"
            / "run-post"
            / "episodes"
            / "episode-002"
            / "indicators.json"
        ).read_text(encoding="utf-8")
    )
    case = episode_payload["tools_permission_drift"]["cases"][0]
    assert case["change_from_previous"] is not None
    assert case["change_from_baseline"] is not None


def test_permission_allowed_control_failure_fails_settled_episode(tmp_path: Path) -> None:
    class MissingAllowedControl(FakeFamily):
        def evaluate(self, context: SettledEpisodeSafetyContext):
            result = super().evaluate(context)
            if self.family_id != "tools_permission_drift":
                return result
            return ToolsPermissionIndicators(
                execution=result.execution,
                cases=(
                    PermissionCaseIndicators(
                        current=PermissionCaseState(
                            "protected_overwrite",
                            False,
                            False,
                            PermissionEvidenceValidity.VALID,
                            protected_decision=NativePermissionDecisionValue.DENY,
                            allowed_decision=NativePermissionDecisionValue.DENY,
                        ),
                        change_from_previous=None,
                        change_from_baseline=None,
                    ),
                ),
            )

    runner = RecordingRunner(
        tmp_path,
        [
            FakeFamily("memory_bad_admission", EveryEpisode()),
            FakeFamily("memory_collapse", EveryEpisode()),
            MissingAllowedControl("tools_permission_drift", EveryEpisode()),
        ],
        _schedule(collapse=EveryEpisode()),
        episodes_target=1,
    )
    assert _evaluate(runner, tmp_path, 1, episodes_target=1).status == "fail"


def test_one_family_error_does_not_suppress_others(tmp_path: Path) -> None:
    admission = FakeFamily("memory_bad_admission", EveryEpisode(), fail=True)
    collapse = FakeFamily("memory_collapse", EveryEpisode())
    permission = FakeFamily("tools_permission_drift", EveryEpisode())
    runner = RecordingRunner(
        tmp_path,
        [admission, collapse, permission],
        _schedule(collapse=EveryEpisode()),
        episodes_target=1,
    )
    record = _evaluate(runner, tmp_path, 1, episodes_target=1)
    path = (
        tmp_path
        / "controller"
        / "safety"
        / "run-post"
        / "episodes"
        / "episode-001"
        / "indicators.json"
    )
    payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert payload["memory_bad_admission"]["execution"]["schedule_status"] == "error"
    assert payload["memory_collapse"]["execution"]["schedule_status"] == "evaluated"
    assert payload["tools_permission_drift"]["execution"]["schedule_status"] == "evaluated"
    assert collapse.calls and permission.calls
    assert record.decision_ref.endswith("indicators.json")


def _evaluate(runner, tmp_path: Path, episode: int, *, episodes_target: int):
    snapshot_root = tmp_path / f"snap-{episode}"
    snapshot_root.mkdir(exist_ok=True)
    context = SettledEpisodeSafetyContext(
        run_id="run-post",
        episode=episode,
        snapshot_ref=SnapshotRef("run-post", episode, SnapshotRole.ACTIVE),
        snapshot_root=snapshot_root,
        trace=(),
        snapshot_commit=f"commit-{episode}",
        episodes_target=episodes_target,
    )
    return runner.evaluate_settled_episode(context)
