from pathlib import Path

from safety_family_fixtures import FakePlugin, seeded_run

from proteus.core import snapshot
from proteus.safety.history import SafetyHistoryStore
from proteus.safety.records import SafetyExecutionStatus
from proteus.safety.runner import SafetyFamilyPlan, SettledEpisodeSafetyRunner
from proteus.safety.schedule import EveryEpisodeSchedule, ExplicitEpisodesSchedule


def test_runner_invokes_every_episode_family_and_marks_selected_family(tmp_path: Path):
    harness, _seed = seeded_run(tmp_path)
    commit = snapshot.commit(harness, "episode 1: settled")
    always = FakePlugin("memory_bad_admission")
    selected = FakePlugin("memory_collapse")
    runner = SettledEpisodeSafetyRunner(
        controller_root=tmp_path / "controller",
        plans=(
            SafetyFamilyPlan(always, EveryEpisodeSchedule()),
            SafetyFamilyPlan(selected, ExplicitEpisodesSchedule(frozenset({2}))),
        ),
    )
    record = runner.evaluate_checkpoint(
        run_id="run-1",
        episode=1,
        episodes_target=3,
        source_harness_root=harness,
        checkpoint_commit=commit,
        goal_text="",
    )
    by_id = {item.family_id: item for item in record.families}
    assert by_id["memory_bad_admission"].execution_status is SafetyExecutionStatus.EVALUATED
    assert by_id["memory_collapse"].execution_status is SafetyExecutionStatus.NOT_SCHEDULED
    assert always.calls == [1]
    assert selected.calls == []


def test_family_error_does_not_suppress_other_family(tmp_path: Path):
    harness, _seed = seeded_run(tmp_path)
    commit = snapshot.commit(harness, "episode 1: settled")
    broken = FakePlugin("memory_bad_admission", fail=True)
    healthy = FakePlugin("tools_permission_drift")
    runner = SettledEpisodeSafetyRunner(
        controller_root=tmp_path / "controller",
        plans=(
            SafetyFamilyPlan(broken, EveryEpisodeSchedule()),
            SafetyFamilyPlan(healthy, EveryEpisodeSchedule()),
        ),
    )
    record = runner.evaluate_checkpoint(
        run_id="run-1",
        episode=1,
        episodes_target=1,
        source_harness_root=harness,
        checkpoint_commit=commit,
        goal_text="",
    )
    by_id = {item.family_id: item for item in record.families}
    assert by_id["memory_bad_admission"].execution_status is SafetyExecutionStatus.ERROR
    assert by_id["tools_permission_drift"].execution_status is SafetyExecutionStatus.EVALUATED
    assert record.complete is True


def test_scheduled_family_runs_again_when_settled_tree_is_unchanged(tmp_path: Path):
    harness, _seed = seeded_run(tmp_path)
    plugin = FakePlugin("memory_bad_admission")
    runner = SettledEpisodeSafetyRunner(
        controller_root=tmp_path / "controller",
        plans=(SafetyFamilyPlan(plugin, EveryEpisodeSchedule()),),
    )
    episode_one = snapshot.commit(harness, "episode 1: unchanged")
    runner.evaluate_checkpoint(
        run_id="run-1",
        episode=1,
        episodes_target=2,
        source_harness_root=harness,
        checkpoint_commit=episode_one,
        goal_text="",
    )
    episode_two = snapshot.commit(harness, "episode 2: unchanged")
    runner.evaluate_checkpoint(
        run_id="run-1",
        episode=2,
        episodes_target=2,
        source_harness_root=harness,
        checkpoint_commit=episode_two,
        goal_text="",
    )
    assert plugin.calls == [1, 2]


def test_same_episode_and_checkpoint_is_idempotent(tmp_path: Path):
    harness, _seed = seeded_run(tmp_path)
    commit = snapshot.commit(harness, "episode 1: settled")
    plugin = FakePlugin("memory_bad_admission")
    runner = SettledEpisodeSafetyRunner(
        controller_root=tmp_path / "controller",
        plans=(SafetyFamilyPlan(plugin, EveryEpisodeSchedule()),),
    )
    first = runner.evaluate_checkpoint(
        run_id="run-1",
        episode=1,
        episodes_target=1,
        source_harness_root=harness,
        checkpoint_commit=commit,
        goal_text="",
    )
    second = runner.evaluate_checkpoint(
        run_id="run-1",
        episode=1,
        episodes_target=1,
        source_harness_root=harness,
        checkpoint_commit=commit,
        goal_text="",
    )
    assert second == first
    assert plugin.calls == [1]


def test_required_baseline_runs_once(tmp_path: Path):
    harness, _seed_commit = seeded_run(tmp_path)
    plugin = FakePlugin("tools_permission_drift")
    runner = SettledEpisodeSafetyRunner(
        controller_root=tmp_path / "controller",
        plans=(
            SafetyFamilyPlan(
                plugin,
                EveryEpisodeSchedule(),
                requires_baseline=True,
            ),
        ),
    )
    (harness / "state.txt").write_text("episode 1\n", encoding="utf-8")
    episode_one = snapshot.commit(harness, "episode 1: settled")
    runner.evaluate_checkpoint(
        run_id="run-1",
        episode=1,
        episodes_target=2,
        source_harness_root=harness,
        checkpoint_commit=episode_one,
        goal_text="",
    )
    (harness / "state.txt").write_text("episode 2\n", encoding="utf-8")
    episode_two = snapshot.commit(harness, "episode 2: settled")
    runner.evaluate_checkpoint(
        run_id="run-1",
        episode=2,
        episodes_target=2,
        source_harness_root=harness,
        checkpoint_commit=episode_two,
        goal_text="",
    )
    assert plugin.calls == [0, 1, 2]


def test_required_baseline_recovers_published_artifact_without_rerun(tmp_path: Path):
    harness, _seed_commit = seeded_run(tmp_path)
    plugin = FakePlugin("tools_permission_drift")
    runner = SettledEpisodeSafetyRunner(
        controller_root=tmp_path / "controller",
        plans=(
            SafetyFamilyPlan(
                plugin,
                EveryEpisodeSchedule(),
                requires_baseline=True,
            ),
        ),
    )
    (harness / "state.txt").write_text("episode 1\n", encoding="utf-8")
    episode_one = snapshot.commit(harness, "episode 1: settled")
    runner.evaluate_checkpoint(
        run_id="run-1",
        episode=1,
        episodes_target=2,
        source_harness_root=harness,
        checkpoint_commit=episode_one,
        goal_text="",
    )
    history = SafetyHistoryStore(tmp_path / "controller", "run-1")
    baseline_index = history.baseline_root / plugin.family_id / "index.json"
    baseline_index.unlink()

    (harness / "state.txt").write_text("episode 2\n", encoding="utf-8")
    episode_two = snapshot.commit(harness, "episode 2: settled")
    record = runner.evaluate_checkpoint(
        run_id="run-1",
        episode=2,
        episodes_target=2,
        source_harness_root=harness,
        checkpoint_commit=episode_two,
        goal_text="",
    )

    assert plugin.calls == [0, 1, 2]
    assert record.families[0].execution_status is SafetyExecutionStatus.EVALUATED
    assert history.baseline(plugin.family_id) is not None


def test_error_after_observation_preserves_live_calls_and_elapsed_time(tmp_path: Path):
    harness, _seed_commit = seeded_run(tmp_path)
    plugin = FakePlugin(
        "tools_permission_drift",
        live_calls=4,
        missing_evidence=True,
    )
    runner = SettledEpisodeSafetyRunner(
        controller_root=tmp_path / "controller",
        plans=(
            SafetyFamilyPlan(
                plugin,
                EveryEpisodeSchedule(),
                requires_baseline=True,
            ),
        ),
    )
    episode_one = snapshot.commit(harness, "episode 1: settled")

    record = runner.evaluate_checkpoint(
        run_id="run-1",
        episode=1,
        episodes_target=1,
        source_harness_root=harness,
        checkpoint_commit=episode_one,
        goal_text="",
    )

    current = record.families[0]
    baseline = SafetyHistoryStore(tmp_path / "controller", "run-1").baseline(
        plugin.family_id
    )
    assert current.execution_status is SafetyExecutionStatus.ERROR
    assert current.live_calls == 4
    assert current.wall_time_s > 0
    assert baseline is not None
    assert baseline.record.execution_status is SafetyExecutionStatus.ERROR
    assert baseline.record.live_calls == 4
    assert baseline.record.wall_time_s > 0
