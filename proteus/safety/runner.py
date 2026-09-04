"""Isolated safety evaluation for one durable settled-episode checkpoint."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from proteus.core import snapshot
from proteus.safety.history import FamilyHistoryEntry, SafetyHistoryStore
from proteus.safety.publication import AtomicSafetyPublication, write_json
from proteus.safety.records import (
    EpisodeSafetyRecord,
    FamilyEvaluationContext,
    FamilyExecutionRecord,
    SafetyExecutionStatus,
    SafetyFamilyObservation,
    SettledEpisodeSafetyContext,
    SettledSnapshotRef,
)
from proteus.safety.schedule import FamilySchedule
from proteus.safety.taxonomy import SafetyStatus


class SafetyFamilyPlugin(Protocol):
    family_id: str
    family_version: str
    live_call_cap: int

    def evaluate(self, context: FamilyEvaluationContext) -> SafetyFamilyObservation: ...

    def compare(
        self,
        previous: SafetyFamilyObservation | None,
        baseline: SafetyFamilyObservation | None,
        current: SafetyFamilyObservation,
    ) -> object: ...

    def load(self, path: Path) -> SafetyFamilyObservation: ...


@dataclass(frozen=True)
class SafetyFamilyPlan:
    plugin: SafetyFamilyPlugin
    schedule: FamilySchedule
    probe_seed: str = ""
    requires_baseline: bool = False


def _execution_status(observation: SafetyFamilyObservation) -> SafetyExecutionStatus:
    if not observation.evidence_complete:
        return SafetyExecutionStatus.NOT_EVALUATED
    if observation.terminal_status in {SafetyStatus.PASS, SafetyStatus.FAIL}:
        return SafetyExecutionStatus.EVALUATED
    raise ValueError("complete family evidence requires a pass or fail terminal status")


class SettledEpisodeSafetyRunner:
    """Evaluate configured families once after an episode checkpoint settles."""

    def __init__(self, *, controller_root: Path, plans: tuple[SafetyFamilyPlan, ...]) -> None:
        family_ids = tuple(plan.plugin.family_id for plan in plans)
        if not plans or len(family_ids) != len(set(family_ids)):
            raise ValueError("settled safety runner requires unique family plans")
        self.controller_root = Path(controller_root)
        self.plans = plans

    def evaluate_checkpoint(
        self,
        *,
        run_id: str,
        episode: int,
        episodes_target: int,
        source_harness_root: Path,
        checkpoint_commit: str,
        goal_text: str,
    ) -> EpisodeSafetyRecord:
        history = SafetyHistoryStore(self.controller_root, run_id)
        existing = history.record_for_episode(episode)
        if existing is not None:
            if existing.checkpoint_commit != checkpoint_commit:
                raise ValueError(
                    "settled safety episode already exists for a different checkpoint"
                )
            return existing

        # Recover a crash after artifact publication but before the history append.
        published = history.published_record(episode)
        if published is not None:
            if published.checkpoint_commit != checkpoint_commit:
                raise ValueError("published safety episode belongs to a different checkpoint")
            history.append(published)
            return published

        records = history.records()
        expected_episode = len(records) + 1
        if episode != expected_episode:
            raise ValueError(
                f"settled safety history expected episode {expected_episode}, got {episode}"
            )
        if snapshot.commit_for_episode(source_harness_root, episode) != checkpoint_commit:
            raise ValueError("safety checkpoint does not match the durable episode mapping")

        settled = SettledEpisodeSafetyContext(
            run_id=run_id,
            episode=episode,
            episodes_target=episodes_target,
            snapshot=SettledSnapshotRef(run_id, episode),
            checkpoint_commit=checkpoint_commit,
            source_harness_root=source_harness_root,
            goal_text=goal_text,
            controller_root=self.controller_root,
        )
        final_root = history.root / f"episode-{episode:03d}"
        final_summary = final_root / "summary.json"

        with AtomicSafetyPublication(
            final_root,
            label="settled episode safety artifact",
        ) as publication:
            assert publication.staging_root is not None
            staging = publication.staging_root
            family_records: list[FamilyExecutionRecord] = []

            for plan in self.plans:
                previous = history.last_observed(plan.plugin.family_id)
                if not plan.schedule.selected(
                    episode=episode,
                    episodes_target=episodes_target,
                ):
                    record = FamilyExecutionRecord.not_scheduled(
                        family_id=plan.plugin.family_id,
                        family_version=plan.plugin.family_version,
                        episode=episode,
                        last_observed_episode=(previous.episode if previous else None),
                    )
                    self._write_execution(staging, record)
                    family_records.append(record)
                    continue

                try:
                    baseline = history.baseline(plan.plugin.family_id)
                    if plan.requires_baseline and baseline is None:
                        baseline = self._evaluate_baseline(
                            plan=plan,
                            settled=settled,
                            history=history,
                        )
                    record = self._evaluate_family(
                        plan=plan,
                        settled=settled,
                        staging_root=staging,
                        final_root=final_root,
                        previous=previous,
                        baseline=baseline,
                    )
                except Exception as exc:  # noqa: BLE001 - isolate each family executor
                    record = FamilyExecutionRecord(
                        family_id=plan.plugin.family_id,
                        family_version=plan.plugin.family_version,
                        episode=episode,
                        execution_status=SafetyExecutionStatus.ERROR,
                        last_observed_episode=(previous.episode if previous else None),
                        reason=f"{type(exc).__name__}: {str(exc)[:1000]}",
                    )
                    self._write_execution(staging, record)
                family_records.append(record)

            episode_record = EpisodeSafetyRecord(
                run_id=run_id,
                episode=episode,
                snapshot=settled.snapshot,
                checkpoint_commit=checkpoint_commit,
                families=tuple(family_records),
                complete=True,
                artifact_ref=final_summary.relative_to(self.controller_root).as_posix(),
            )
            write_json(staging / "summary.json", episode_record)
            publication.publish()

        history.append(episode_record)
        return episode_record

    def publish_controller_error(
        self,
        *,
        run_id: str,
        episode: int,
        checkpoint_commit: str,
        reason: str,
    ) -> EpisodeSafetyRecord:
        """Publish the durable minimal envelope when the controller itself fails."""
        history = SafetyHistoryStore(self.controller_root, run_id)
        existing = history.record_for_episode(episode)
        if existing is not None:
            if existing.checkpoint_commit != checkpoint_commit:
                raise ValueError("controller-error record has a checkpoint mismatch")
            return existing
        published = history.published_record(episode)
        if published is not None:
            if published.checkpoint_commit != checkpoint_commit:
                raise ValueError("published safety record has a checkpoint mismatch")
            history.append(published)
            return published

        final_root = history.root / f"episode-{episode:03d}"
        with AtomicSafetyPublication(
            final_root,
            label="settled episode safety controller error",
        ) as publication:
            assert publication.staging_root is not None
            staging = publication.staging_root
            families = tuple(
                FamilyExecutionRecord(
                    family_id=plan.plugin.family_id,
                    family_version=plan.plugin.family_version,
                    episode=episode,
                    execution_status=SafetyExecutionStatus.ERROR,
                    reason=f"controller_error:{reason}",
                )
                for plan in self.plans
            )
            for record in families:
                self._write_execution(staging, record)
            result = EpisodeSafetyRecord(
                run_id=run_id,
                episode=episode,
                snapshot=SettledSnapshotRef(run_id, episode),
                checkpoint_commit=checkpoint_commit,
                families=families,
                complete=False,
                artifact_ref=(final_root / "summary.json")
                .relative_to(self.controller_root)
                .as_posix(),
            )
            write_json(staging / "summary.json", result)
            publication.publish()
        history.append(result)
        return result

    def reconcile_checkpoint(
        self,
        *,
        run_id: str,
        completed_episode: int,
        episodes_target: int,
        source_harness_root: Path,
        goal_text: str,
    ) -> EpisodeSafetyRecord | None:
        """Fill the sole allowed gap between ordinary and safety histories."""
        if completed_episode == 0:
            return None
        history = SafetyHistoryStore(self.controller_root, run_id)
        records = history.records()
        if len(records) == completed_episode:
            return records[-1]
        if len(records) != completed_episode - 1:
            raise ValueError(
                "ordinary and safety histories differ by more than one settled episode"
            )
        checkpoint_commit = snapshot.commit_for_episode(
            source_harness_root,
            completed_episode,
        )
        if checkpoint_commit is None:
            raise ValueError("resume safety reconciliation is missing the checkpoint")
        return self.evaluate_checkpoint(
            run_id=run_id,
            episode=completed_episode,
            episodes_target=episodes_target,
            source_harness_root=source_harness_root,
            checkpoint_commit=checkpoint_commit,
            goal_text=goal_text,
        )

    def _write_execution(
        self,
        staging_root: Path,
        record: FamilyExecutionRecord,
    ) -> None:
        write_json(
            staging_root / "families" / record.family_id / "execution.json",
            record,
        )

    def _load_observation(
        self,
        plan: SafetyFamilyPlan,
        entry: FamilyHistoryEntry | None,
    ) -> SafetyFamilyObservation | None:
        if entry is None or not entry.record.observation_ref:
            return None
        return plan.plugin.load(self.controller_root / entry.record.observation_ref)

    def _validate_observation(
        self,
        *,
        plan: SafetyFamilyPlan,
        settled: SettledEpisodeSafetyContext,
        observation: SafetyFamilyObservation,
        artifact_root: Path,
    ) -> None:
        if observation.family_id != plan.plugin.family_id:
            raise ValueError("family observation has the wrong family ID")
        if observation.family_version != plan.plugin.family_version:
            raise ValueError("family observation has the wrong family version")
        if observation.snapshot != settled.snapshot:
            raise ValueError("family observation has the wrong settled snapshot")
        canonical_root = artifact_root.resolve()
        for ref in observation.evidence_refs:
            path = (artifact_root / ref).resolve()
            try:
                path.relative_to(canonical_root)
            except ValueError as exc:
                raise ValueError("family evidence escapes the episode artifact") from exc
            if not path.is_file():
                raise ValueError(f"missing direct family evidence: {ref}")

    def _evaluate_family(
        self,
        *,
        plan: SafetyFamilyPlan,
        settled: SettledEpisodeSafetyContext,
        staging_root: Path,
        final_root: Path,
        previous: FamilyHistoryEntry | None,
        baseline: FamilyHistoryEntry | None,
    ) -> FamilyExecutionRecord:
        family_staging = staging_root / "families" / plan.plugin.family_id
        snapshot_root = staging_root / "trials" / plan.plugin.family_id / "harness"
        snapshot.materialize(
            settled.source_harness_root,
            settled.checkpoint_commit,
            snapshot_root,
        )
        context = FamilyEvaluationContext(
            settled=settled,
            snapshot_root=snapshot_root,
            trial_root=snapshot_root.parent,
            artifact_root=staging_root,
            evidence_dir=family_staging / "evidence",
            probe_seed=plan.probe_seed,
        )
        started = time.monotonic()
        observation: SafetyFamilyObservation | None = None
        wall_time_s = 0.0
        try:
            observation = plan.plugin.evaluate(context)
            wall_time_s = time.monotonic() - started
            self._validate_observation(
                plan=plan,
                settled=settled,
                observation=observation,
                artifact_root=staging_root,
            )

            previous_observation = self._load_observation(plan, previous)
            baseline_observation = self._load_observation(plan, baseline)
            delta = plan.plugin.compare(
                previous_observation,
                baseline_observation,
                observation,
            )
            write_json(family_staging / "observation.json", observation)
            write_json(family_staging / "delta.json", delta)

            canonical_family_root = final_root / "families" / plan.plugin.family_id
            record = FamilyExecutionRecord(
                family_id=plan.plugin.family_id,
                family_version=plan.plugin.family_version,
                episode=settled.episode,
                execution_status=_execution_status(observation),
                observation_ref=(canonical_family_root / "observation.json")
                .relative_to(self.controller_root)
                .as_posix(),
                delta_ref=(canonical_family_root / "delta.json")
                .relative_to(self.controller_root)
                .as_posix(),
                last_observed_episode=(previous.episode if previous else None),
                reason=(
                    ""
                    if observation.evidence_complete
                    else "scheduled_family_evidence_incomplete"
                ),
                live_calls=observation.live_calls,
                wall_time_s=wall_time_s,
            )
        except Exception as exc:  # noqa: BLE001 - retain known family accounting
            wall_time_s = time.monotonic() - started
            record = FamilyExecutionRecord(
                family_id=plan.plugin.family_id,
                family_version=plan.plugin.family_version,
                episode=settled.episode,
                execution_status=SafetyExecutionStatus.ERROR,
                last_observed_episode=(previous.episode if previous else None),
                reason=f"{type(exc).__name__}: {str(exc)[:1000]}",
                live_calls=(observation.live_calls if observation is not None else 0),
                wall_time_s=wall_time_s,
            )
        self._write_execution(staging_root, record)
        return record

    def _evaluate_baseline(
        self,
        *,
        plan: SafetyFamilyPlan,
        settled: SettledEpisodeSafetyContext,
        history: SafetyHistoryStore,
    ) -> FamilyHistoryEntry:
        seed_commit = snapshot.commit_for_episode(settled.source_harness_root, 0)
        if seed_commit is None:
            raise ValueError("safety baseline requires episode-0 checkpoint")
        published = history.published_baseline(
            plan.plugin.family_id,
            checkpoint_commit=seed_commit,
        )
        if published is not None:
            if published.record.family_version != plan.plugin.family_version:
                raise ValueError("published safety baseline has the wrong family version")
            history.write_baseline(published)
            return published
        baseline_settled = SettledEpisodeSafetyContext(
            run_id=settled.run_id,
            episode=0,
            episodes_target=settled.episodes_target,
            snapshot=SettledSnapshotRef(settled.run_id, 0),
            checkpoint_commit=seed_commit,
            source_harness_root=settled.source_harness_root,
            goal_text=settled.goal_text,
            controller_root=settled.controller_root,
        )
        final_family_root = (
            history.root / "episode-000" / "families" / plan.plugin.family_id
        )
        with AtomicSafetyPublication(
            final_family_root,
            label=f"{plan.plugin.family_id} safety baseline",
        ) as publication:
            assert publication.staging_root is not None
            staging_family = publication.staging_root
            snapshot_root = staging_family / "trial" / "harness"
            snapshot.materialize(
                baseline_settled.source_harness_root,
                seed_commit,
                snapshot_root,
            )
            context = FamilyEvaluationContext(
                settled=baseline_settled,
                snapshot_root=snapshot_root,
                trial_root=snapshot_root.parent,
                artifact_root=staging_family,
                evidence_dir=staging_family / "evidence",
                probe_seed=plan.probe_seed,
            )
            started = time.monotonic()
            observation: SafetyFamilyObservation | None = None
            wall_time_s = 0.0
            try:
                observation = plan.plugin.evaluate(context)
                wall_time_s = time.monotonic() - started
                self._validate_observation(
                    plan=plan,
                    settled=baseline_settled,
                    observation=observation,
                    artifact_root=staging_family,
                )
                write_json(staging_family / "observation.json", observation)
                execution_status = _execution_status(observation)
                reason = "" if observation.evidence_complete else "baseline_evidence_incomplete"
                observation_ref = (final_family_root / "observation.json")
                observation_ref = observation_ref.relative_to(self.controller_root).as_posix()
                live_calls = observation.live_calls
            except Exception as exc:  # noqa: BLE001 - baseline is family-owned evidence
                wall_time_s = time.monotonic() - started
                execution_status = SafetyExecutionStatus.ERROR
                reason = f"{type(exc).__name__}: {str(exc)[:1000]}"
                observation_ref = ""
                live_calls = observation.live_calls if observation is not None else 0

            record = FamilyExecutionRecord(
                family_id=plan.plugin.family_id,
                family_version=plan.plugin.family_version,
                episode=0,
                execution_status=execution_status,
                observation_ref=observation_ref,
                reason=reason,
                live_calls=live_calls,
                wall_time_s=wall_time_s,
            )
            write_json(staging_family / "execution.json", record)
            publication.publish()

        entry = FamilyHistoryEntry(
            episode=0,
            checkpoint_commit=seed_commit,
            record=record,
        )
        history.write_baseline(entry)
        return entry
