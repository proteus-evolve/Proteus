from pathlib import Path

import pytest

from proteus.safety.history import FamilyHistoryEntry, SafetyHistoryStore
from proteus.safety.indicators import SafetyChange, classify_status_change
from proteus.safety.records import (
    EpisodeSafetyRecord,
    FamilyExecutionRecord,
    SafetyExecutionStatus,
    SettledSnapshotRef,
)
from proteus.safety.taxonomy import SafetyStatus


def test_status_change_table_is_exact():
    assert classify_status_change(SafetyStatus.PASS, SafetyStatus.PASS) is SafetyChange.STABLE_SAFE
    assert classify_status_change(SafetyStatus.PASS, SafetyStatus.FAIL) is SafetyChange.REGRESSION
    assert classify_status_change(SafetyStatus.FAIL, SafetyStatus.PASS) is SafetyChange.REPAIR
    assert classify_status_change(SafetyStatus.FAIL, SafetyStatus.FAIL) is SafetyChange.PERSISTENT_UNSAFE
    assert classify_status_change(SafetyStatus.NOT_EVALUATED, SafetyStatus.PASS) is SafetyChange.UNAVAILABLE


def episode_record(episode: int, status: SafetyExecutionStatus) -> EpisodeSafetyRecord:
    family = FamilyExecutionRecord(
        family_id="memory_bad_admission",
        family_version="3",
        episode=episode,
        execution_status=status,
        observation_ref=(f"episode-{episode:03d}/observation.json" if status is SafetyExecutionStatus.EVALUATED else ""),
    )
    return EpisodeSafetyRecord(
        run_id="run-1",
        episode=episode,
        snapshot=SettledSnapshotRef("run-1", episode),
        checkpoint_commit=f"commit-{episode}",
        families=(family,),
        complete=True,
        artifact_ref=f"episode-{episode:03d}/summary.json",
    )


def test_history_ignores_non_evaluated_records_and_reloads(tmp_path: Path):
    store = SafetyHistoryStore(tmp_path, "run-1")
    store.append(episode_record(1, SafetyExecutionStatus.EVALUATED))
    store.append(episode_record(2, SafetyExecutionStatus.NOT_SCHEDULED))
    store.append(episode_record(3, SafetyExecutionStatus.NOT_EVALUATED))
    store.append(episode_record(4, SafetyExecutionStatus.ERROR))

    reloaded = SafetyHistoryStore(tmp_path, "run-1")
    assert reloaded.last_observed("memory_bad_admission").episode == 1
    assert tuple(record.episode for record in reloaded.records()) == (1, 2, 3, 4)


def test_history_rejects_out_of_order_episode(tmp_path: Path):
    store = SafetyHistoryStore(tmp_path, "run-1")
    with pytest.raises(ValueError, match="expected episode 1, got 2"):
        store.append(episode_record(2, SafetyExecutionStatus.EVALUATED))


def test_history_stores_episode_zero_baseline_separately(tmp_path: Path):
    store = SafetyHistoryStore(tmp_path, "run-1")
    baseline = FamilyHistoryEntry(
        episode=0,
        checkpoint_commit="seed-commit",
        record=FamilyExecutionRecord(
            family_id="tools_permission_drift",
            family_version="3",
            episode=0,
            execution_status=SafetyExecutionStatus.EVALUATED,
            observation_ref="episode-000/tools_permission_drift/observation.json",
        ),
    )
    store.write_baseline(baseline)

    assert SafetyHistoryStore(tmp_path, "run-1").baseline("tools_permission_drift") == baseline
    assert store.records() == ()
