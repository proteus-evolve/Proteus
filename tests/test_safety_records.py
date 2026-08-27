from pathlib import Path

from proteus.safety.records import (
    EpisodeSafetyRecord,
    FamilyExecutionRecord,
    SafetyExecutionStatus,
    SettledEpisodeSafetyContext,
    SettledSnapshotRef,
)


def test_settled_snapshot_has_no_candidate_role():
    ref = SettledSnapshotRef("run-1", 3)
    assert ref.to_dict() == {"run_id": "run-1", "episode": 3}
    assert not hasattr(ref, "role")


def test_not_scheduled_record_has_no_observation_ref():
    record = FamilyExecutionRecord.not_scheduled(
        family_id="memory_collapse",
        family_version="3",
        episode=2,
        last_observed_episode=1,
    )
    assert record.execution_status is SafetyExecutionStatus.NOT_SCHEDULED
    assert record.observation_ref == ""
    assert record.last_observed_episode == 1


def test_context_identifies_one_settled_checkpoint(tmp_path: Path):
    context = SettledEpisodeSafetyContext(
        run_id="run-1",
        episode=2,
        episodes_target=10,
        snapshot=SettledSnapshotRef("run-1", 2),
        checkpoint_commit="abc123",
        source_harness_root=tmp_path / "harness",
        goal_text="goal",
        controller_root=tmp_path / "controller",
    )
    assert context.snapshot.episode == context.episode
