import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from safety_family_fixtures import FakePlugin
from test_candidate_activation import _cfg

from proteus.adapters.minimal import MinimalHarness
from proteus.core import NEUTRAL, GoalConfig, snapshot
from proteus.core.episode import run
from proteus.safety.history import SafetyHistoryStore
from proteus.safety.records import (
    EpisodeSafetyRecord,
    SafetyExecutionStatus,
    SettledSnapshotRef,
)
from proteus.safety.runner import SafetyFamilyPlan, SettledEpisodeSafetyRunner
from proteus.safety.schedule import EveryEpisodeSchedule
from proteus.sweep import SweepConfig, opaque_id, run_sweep


def actual_runner(tmp_path: Path) -> SettledEpisodeSafetyRunner:
    return SettledEpisodeSafetyRunner(
        controller_root=tmp_path / "controller",
        plans=(
            SafetyFamilyPlan(FakePlugin("memory_bad_admission"), EveryEpisodeSchedule()),
        ),
    )


@dataclass
class RecordingSafetyRunner:
    contexts: list[tuple[int, str]]

    def evaluate_checkpoint(
        self,
        *,
        run_id,
        episode,
        episodes_target,
        source_harness_root,
        checkpoint_commit,
        goal_text,
    ):
        del episodes_target, goal_text
        assert snapshot.commit_for_episode(source_harness_root, episode) == checkpoint_commit
        self.contexts.append((episode, checkpoint_commit))
        return EpisodeSafetyRecord(
            run_id=run_id,
            episode=episode,
            snapshot=SettledSnapshotRef(run_id, episode),
            checkpoint_commit=checkpoint_commit,
            families=(),
            complete=True,
            artifact_ref=f"safety-episodes/{run_id}/episode-{episode:03d}/summary.json",
        )


def test_safety_runs_once_after_each_settled_episode(tmp_path):
    runner = RecordingSafetyRunner([])
    result = run(_cfg(tmp_path, episodes=3, safety_runner=runner))

    assert [episode for episode, _ in runner.contexts] == [1, 2, 3]
    assert [row["safety_ref"] for row in result.eval_history] == [
        f"safety-episodes/run-candidate-test/episode-{episode:03d}/summary.json"
        for episode in (1, 2, 3)
    ]
    assert [row["safety_complete"] for row in result.eval_history] == [True, True, True]


def test_existing_post_episode_runner_remains_a_temporary_settled_seam(tmp_path):
    class ExistingPostEpisodeRunner:
        def __init__(self) -> None:
            self.episodes = []

        def evaluate_settled_episode(self, context):
            self.episodes.append(context.episode)
            return SimpleNamespace(
                status="pass",
                decision_ref=f"legacy/episode-{context.episode:03d}.json",
            )

    runner = ExistingPostEpisodeRunner()
    result = run(_cfg(tmp_path, episodes=1, safety_runner=runner))

    assert runner.episodes == [1]
    assert result.eval_history[0]["safety_ref"] == "legacy/episode-001.json"
    assert result.eval_history[0]["safety_complete"] is True


def test_safety_error_does_not_stop_next_episode(tmp_path):
    class BrokenRunner:
        def __init__(self) -> None:
            self.calls = []

        def evaluate_checkpoint(self, **kwargs):
            self.calls.append(kwargs["episode"])
            raise RuntimeError("safety unavailable")

        def publish_controller_error(self, **kwargs):
            return EpisodeSafetyRecord(
                run_id=kwargs["run_id"],
                episode=kwargs["episode"],
                snapshot=SettledSnapshotRef(kwargs["run_id"], kwargs["episode"]),
                checkpoint_commit=kwargs["checkpoint_commit"],
                families=(),
                complete=False,
                artifact_ref=(
                    f"safety-episodes/{kwargs['run_id']}/"
                    f"episode-{kwargs['episode']:03d}/summary.json"
                ),
            )

    runner = BrokenRunner()
    result = run(_cfg(tmp_path, episodes=2, safety_runner=runner))

    assert result.episodes_complete == 2
    assert result.error == ""
    assert runner.calls == [1, 2]
    assert all(row["safety_ref"] for row in result.eval_history)
    assert [row["safety_complete"] for row in result.eval_history] == [False, False]


def test_settled_runner_publishes_durable_controller_error_envelope(tmp_path):
    class BrokenSettledRunner(SettledEpisodeSafetyRunner):
        def evaluate_checkpoint(self, **kwargs):
            raise RuntimeError("controller unavailable")

    runner = BrokenSettledRunner(
        controller_root=tmp_path / "controller",
        plans=(
            SafetyFamilyPlan(FakePlugin("memory_bad_admission"), EveryEpisodeSchedule()),
        ),
    )
    result = run(_cfg(tmp_path, episodes=1, safety_runner=runner))
    store = SafetyHistoryStore(tmp_path / "controller", "run-candidate-test")

    assert result.episodes_complete == 1
    assert result.error == ""
    assert result.eval_history[0]["safety_complete"] is False
    records = store.records()
    assert len(records) == 1
    assert records[0].families[0].execution_status is SafetyExecutionStatus.ERROR
    assert (tmp_path / "controller" / result.eval_history[0]["safety_ref"]).is_file()


def test_resume_completes_missing_safety_record_before_next_episode(tmp_path):
    runner = actual_runner(tmp_path)
    run(_cfg(tmp_path, episodes=1, safety_runner=runner))
    run_id = "run-candidate-test"
    store = SafetyHistoryStore(tmp_path / "controller", run_id)
    store.path.unlink()
    shutil.rmtree(store.root / "episode-001")

    resumed = run(
        _cfg(tmp_path, episodes=2, safety_runner=runner),
        start=1,
        resume=True,
    )

    assert [record.episode for record in store.records()] == [1, 2]
    assert resumed.episodes_complete == 2


def test_sweep_safety_runner_factory_receives_run_id_and_seed(tmp_path):
    calls = []

    def factory(run_id, seed):
        calls.append((run_id, seed))
        return RecordingSafetyRunner([])

    run_sweep(SweepConfig(
        name="factory-test",
        adapter_factory=MinimalHarness,
        arms=(NEUTRAL,),
        seeds=1,
        goal=GoalConfig.no_goal(),
        root=tmp_path / "sweep",
        episodes=1,
        safety_runner_factory=factory,
    ))

    assert calls == [(opaque_id("neutral", 0), 0)]


def test_sweep_overwrite_discards_settled_safety_history(tmp_path):
    root = tmp_path / "sweep"
    plugins = []

    def factory(run_id, seed):
        del seed
        plugin = FakePlugin("memory_bad_admission")
        plugins.append(plugin)
        return SettledEpisodeSafetyRunner(
            controller_root=root,
            plans=(SafetyFamilyPlan(plugin, EveryEpisodeSchedule()),),
        )

    cfg = SweepConfig(
        name="overwrite-safety",
        adapter_factory=MinimalHarness,
        arms=(NEUTRAL,),
        seeds=1,
        goal=GoalConfig.no_goal(),
        root=root,
        episodes=1,
        safety_runner_factory=factory,
    )
    run_sweep(cfg)
    cfg.on_existing = "overwrite"
    run_sweep(cfg)

    assert [plugin.calls for plugin in plugins] == [[1], [1]]
