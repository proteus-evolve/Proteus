"""Replay preserved snapshot transitions through the universal Phase 1 functions."""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from proteus.core import snapshot
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import ProbeEndpoint, ProbeObservation
from proteus.safety.live import LiveModelChannel
from proteus.safety.permission_adapter import PermissionPolicyAdapter
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS
from proteus.safety.permission_executor import (
    PairedPermissionPolicyExecutor,
    PermissionSnapshotSource,
    TransitionPermissionRequest,
)
from proteus.safety.phase1 import SUITE, TOOLS_PERMISSION_DRIFT
from proteus.safety.phase1_runtime import PHASE1_EXECUTORS, Phase1ExecutionRequest
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.publication import (
    AtomicRetrospectivePublication,
    write_json,
)
from proteus.safety.reporting import PermissionCaseDenominators, denominators_from_family
from proteus.safety.runtime import HarnessSafetyRuntime, LogicalTransitionRecord, RuntimeKind
from proteus.safety.taxonomy import SafetyCaseFamilyDefinition, SafetyStatus
from proteus.sweep import read_seed_records

LiveChannelFactory = Callable[[str, str], LiveModelChannel]
LiveChannelFactoryBuilder = Callable[[Path], LiveChannelFactory]


@dataclass(frozen=True)
class LiveModelConfig:
    """Trusted controller inputs for a model-mediated retrospective replay."""

    model: str
    build_channel_factory: LiveChannelFactoryBuilder

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model-mediated retrospective replay requires a model")
        if not callable(self.build_channel_factory):
            raise TypeError("retrospective live channel factory builder must be callable")


@dataclass(frozen=True)
class RetrospectiveSummary:
    source_sweep: str
    transitions_seen: int
    transitions_eligible: int
    transitions_selected: int
    transitions_attempted: int
    transitions_administered: int
    transitions_evaluated: int
    transitions_failed: int
    transitions_not_evaluated: int
    transitions_errors: int
    family_outcomes: Mapping[str, Mapping[str, int]]
    permission_denominators: PermissionCaseDenominators
    manifest_ref: str
    complete: bool


@dataclass(frozen=True)
class _Transition:
    run_root: Path
    record: LogicalTransitionRecord
    lineage: tuple[LogicalTransitionRecord, ...]


@dataclass(frozen=True)
class _RunExpectation:
    run_id: str
    run_root: Path
    episodes: int
    durable_record_valid: bool = True


@dataclass(frozen=True)
class _ArchiveInventory:
    transitions: tuple[_Transition, ...]
    checkpoint_pairs_expected: int
    checkpoint_pairs_seen: int
    exclusions: tuple[Mapping[str, object], ...]
    source_issues: tuple[Mapping[str, object], ...]

    @property
    def complete(self) -> bool:
        return (
            self.checkpoint_pairs_seen == self.checkpoint_pairs_expected
            and not self.source_issues
        )


def _permission_adapter_for(adapter) -> PermissionPolicyAdapter:
    method = getattr(adapter, "permission_policy_adapter", None)
    if not callable(method):
        raise TypeError(
            f"retrospective adapter {getattr(adapter, 'name', type(adapter).__name__)!r} "
            "must implement permission_policy_adapter()"
        )
    permission_adapter = method()
    if not isinstance(permission_adapter, PermissionPolicyAdapter):
        raise TypeError(
            "adapter permission_policy_adapter() does not implement PermissionPolicyAdapter"
        )
    return permission_adapter


def _empty_permission_denominators() -> PermissionCaseDenominators:
    return PermissionCaseDenominators(
        family_id=TOOLS_PERMISSION_DRIFT.family_id,
        family_version="2",
        attempted=0,
        supported=0,
        administered=0,
        evaluated=0,
        passed=0,
        failed=0,
        baseline_failure=0,
        not_evaluated=0,
        invalid=0,
        error=0,
    )


def _add_permission_denominators(
    left: PermissionCaseDenominators, right: PermissionCaseDenominators
) -> PermissionCaseDenominators:
    return PermissionCaseDenominators(
        family_id=left.family_id,
        family_version=left.family_version,
        attempted=left.attempted + right.attempted,
        supported=left.supported + right.supported,
        administered=left.administered + right.administered,
        evaluated=left.evaluated + right.evaluated,
        passed=left.passed + right.passed,
        failed=left.failed + right.failed,
        baseline_failure=left.baseline_failure + right.baseline_failure,
        not_evaluated=left.not_evaluated + right.not_evaluated,
        invalid=left.invalid + right.invalid,
        error=left.error + right.error,
    )


def _runtime_for(adapter) -> HarnessSafetyRuntime:
    method = getattr(adapter, "safety_runtime", None)
    if not callable(method):
        raise TypeError(
            f"retrospective adapter {getattr(adapter, 'name', type(adapter).__name__)!r} "
            "must implement safety_runtime()"
        )
    runtime = method()
    if not isinstance(runtime, HarnessSafetyRuntime):
        raise TypeError("adapter safety_runtime() does not implement HarnessSafetyRuntime")
    return runtime


def _run_expectations(sweep_root: Path) -> tuple[tuple[_RunExpectation, ...], list[dict]]:
    rows = read_seed_records(sweep_root)
    rows_by_id: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        root = Path(str(row.get("root", "")))
        rows_by_id[root.name].append(row)

    issues: list[dict] = []
    manifest_path = sweep_root / "manifest.json"
    manifest: object = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append({"reason": "invalid_sweep_manifest", "error": str(exc)})

    expectations: list[_RunExpectation] = []
    if isinstance(manifest, dict):
        manifest_runs = manifest.get("runs")
        episodes = manifest.get("episodes")
        if (
            isinstance(episodes, int)
            and not isinstance(episodes, bool)
            and episodes >= 1
            and isinstance(manifest_runs, list)
        ):
            for entry in manifest_runs:
                run_id = entry.get("id") if isinstance(entry, dict) else None
                if not isinstance(run_id, str) or not run_id.strip():
                    issues.append({"reason": "invalid_manifest_run"})
                    continue
                matches = rows_by_id.get(run_id, [])
                durable_record_valid = len(matches) == 1
                if not matches:
                    issues.append({"run_id": run_id, "reason": "missing_run_record"})
                elif len(matches) > 1:
                    issues.append({"run_id": run_id, "reason": "duplicate_run_records"})
                else:
                    row = matches[0]
                    episodes_complete = row.get("episodes_complete")
                    if (
                        not isinstance(episodes_complete, int)
                        or isinstance(episodes_complete, bool)
                        or episodes_complete < episodes
                    ):
                        durable_record_valid = False
                        issues.append(
                            {
                                "run_id": run_id,
                                "reason": "short_run",
                                "episodes_complete": episodes_complete,
                                "episodes_expected": episodes,
                            }
                        )
                    elif episodes_complete != episodes:
                        durable_record_valid = False
                        issues.append(
                            {
                                "run_id": run_id,
                                "reason": "run_episode_mismatch",
                                "episodes_complete": episodes_complete,
                                "episodes_expected": episodes,
                            }
                        )
                    run_error = row.get("error")
                    if run_error != "":
                        durable_record_valid = False
                        issues.append(
                            {
                                "run_id": run_id,
                                "reason": "run_error",
                                "error": (
                                    str(run_error)
                                    if run_error is not None
                                    else "missing durable error status"
                                ),
                            }
                        )
                root = (
                    Path(str(matches[0].get("root", "")))
                    if matches
                    else sweep_root / "runs" / run_id
                )
                expectations.append(
                    _RunExpectation(run_id, root, episodes, durable_record_valid)
                )
            if not expectations:
                issues.append({"reason": "no_runs_declared"})
            return tuple(expectations), issues
        issues.append({"reason": "invalid_sweep_manifest_shape"})

    for row in rows:
        root = Path(str(row.get("root", "")))
        try:
            episodes = int(row.get("episodes_complete", 0))
        except (TypeError, ValueError):
            episodes = 0
        if root.name and episodes >= 1:
            expectations.append(_RunExpectation(root.name, root, episodes))
        else:
            issues.append({"run_id": root.name, "reason": "invalid_run_record"})
    if not expectations:
        issues.append({"reason": "no_runs_declared"})
    return tuple(expectations), issues


def _inventory(sweep_root: Path) -> _ArchiveInventory:
    expectations, issues = _run_expectations(sweep_root)
    result: list[_Transition] = []
    exclusions: list[dict] = []
    expected = sum(run.episodes for run in expectations)
    seen = 0
    for run in expectations:
        harness_root = run.run_root / "harness"
        if not run.run_root.is_dir():
            issues.append({"run_id": run.run_id, "reason": "missing_run_root"})
            continue
        if not harness_root.is_dir():
            issues.append({"run_id": run.run_id, "reason": "missing_harness_root"})
            continue
        records: list[LogicalTransitionRecord] = []
        for candidate_episode in range(1, run.episodes + 1):
            active_episode = candidate_episode - 1
            try:
                active_commit = snapshot.commit_for_episode(harness_root, active_episode)
                candidate_commit = snapshot.commit_for_episode(harness_root, candidate_episode)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                issues.append(
                    {
                        "run_id": run.run_id,
                        "active_episode": active_episode,
                        "candidate_episode": candidate_episode,
                        "reason": "invalid_checkpoint_store",
                        "error": str(exc),
                    }
                )
                continue
            missing = []
            if active_commit is None:
                missing.append("active")
            if candidate_commit is None:
                missing.append("candidate")
            if missing:
                issues.append(
                    {
                        "run_id": run.run_id,
                        "active_episode": active_episode,
                        "candidate_episode": candidate_episode,
                        "reason": f"missing_{'_and_'.join(missing)}_checkpoint",
                    }
                )
                continue
            seen += 1
            record = LogicalTransitionRecord(
                active=SnapshotRef(run.run_id, active_episode, SnapshotRole.ACTIVE),
                candidate=SnapshotRef(run.run_id, candidate_episode, SnapshotRole.CANDIDATE),
                activated=None,
                decision_ref="retrospective-supported-only",
            )
            if active_episode == 0:
                exclusions.append(
                    {
                        "active": record.active,
                        "candidate": record.candidate,
                        "reason": "episode_0_seed_has_no_required_native_surfaces",
                    }
                )
            elif run.durable_record_valid:
                result.append(_Transition(run.run_root, record, tuple(records)))
            records.append(record)
    return _ArchiveInventory(
        transitions=tuple(result),
        checkpoint_pairs_expected=expected,
        checkpoint_pairs_seen=seen,
        exclusions=tuple(exclusions),
        source_issues=tuple(issues),
    )


def _select_transitions(
    transitions: tuple[_Transition, ...],
    *,
    run_id: str | None,
    active_episode: int | None,
) -> tuple[tuple[_Transition, ...], Mapping[str, object] | None]:
    if (run_id is None) != (active_episode is None):
        raise ValueError("retrospective --run-id and --active-episode must be provided together")
    if run_id is None:
        return transitions, None
    if not run_id.strip() or not isinstance(active_episode, int) or isinstance(active_episode, bool):
        raise ValueError("retrospective transition selector requires a run ID and integer episode")
    matches = tuple(
        transition
        for transition in transitions
        if transition.record.active.run_id == run_id
        and transition.record.active.episode == active_episode
    )
    if len(matches) != 1:
        raise ValueError(
            "retrospective transition selector must resolve exactly one active/candidate pair"
        )
    return matches, {"run_id": run_id, "active_episode": active_episode}


def _validate_output_location(sweep_root: Path, output_root: Path) -> None:
    resolved_sweep = sweep_root.resolve()
    resolved_output = output_root.resolve()
    if resolved_output == resolved_sweep or resolved_sweep in resolved_output.parents:
        raise ValueError("retrospective output must be outside the preserved sweep root")


def _validate_observation(
    observation: object,
    *,
    definition: SafetyCaseFamilyDefinition,
    snapshot_ref: SnapshotRef,
    endpoint: ProbeEndpoint,
    runtime: HarnessSafetyRuntime,
    artifact_root: Path,
) -> ProbeObservation:
    if not isinstance(observation, ProbeObservation):
        raise TypeError("core safety executor returned malformed evidence")
    if (
        observation.family_id != definition.family_id
        or observation.snapshot != snapshot_ref
        or observation.endpoint is not endpoint
        or observation.runtime_kind is not runtime.kind
    ):
        raise ValueError("core safety executor returned mismatched evidence identity")
    declared = tuple((cell.cell_id, cell.arm, cell.stratum) for cell in definition.declared_cells)
    returned = tuple((cell.cell_id, observation.arm, cell.stratum) for cell in observation.cells)
    if returned != declared:
        raise ValueError("core safety executor did not return the exact declared cells")
    for cell in observation.cells:
        for ref in cell.evidence_refs:
            path = artifact_root / ref
            if not path.is_file():
                raise ValueError(f"missing direct safety evidence: {ref}")
    return observation


def _collect_settled(
    *,
    transition: _Transition,
    definition: SafetyCaseFamilyDefinition,
    adapter,
    artifact_root: Path,
    model: str,
    channel_factory: LiveChannelFactory | None,
) -> ProbeObservation:
    record = transition.record
    source_episode = record.candidate.episode
    snapshot_ref = SnapshotRef(record.candidate.run_id, source_episode, SnapshotRole.ACTIVE)
    trial_root = (
        artifact_root
        / "transitions"
        / record.active.run_id
        / f"episode-{record.active.episode:03d}-to-{record.candidate.episode:03d}"
        / "trials"
        / definition.family_id
        / ProbeEndpoint.SETTLED.value
    )
    snapshot_root = trial_root / "harness"
    source_root = transition.run_root / "harness"
    commit = snapshot.commit_for_episode(source_root, source_episode)
    if commit is None:
        raise ValueError(f"missing preserved checkpoint for episode {source_episode}")
    snapshot.materialize(source_root, commit, snapshot_root)
    context = CandidateSafetyContext(
        run_id=record.active.run_id,
        episode=source_episode,
        adapter_name=getattr(adapter, "name", type(adapter).__name__),
        snapshot=snapshot_ref,
        snapshot_root=snapshot_root,
        trial_root=trial_root,
        evidence_dir=trial_root / "raw-evidence",
        lineage=transition.lineage,
        artifact_root=artifact_root,
        endpoint=ProbeEndpoint.SETTLED,
    )
    runtime = _runtime_for(adapter)
    channel = None
    if runtime.kind is RuntimeKind.MODEL_MEDIATED:
        if channel_factory is None:
            raise ValueError("model-mediated retrospective replay requires live model config")
        channel = channel_factory(
            model,
            f"{record.active.run_id}.episode-{source_episode:03d}."
            f"{definition.family_id}.{ProbeEndpoint.SETTLED.value}",
        )
        if not isinstance(channel, LiveModelChannel):
            raise TypeError("retrospective live channel factory must implement LiveModelChannel")
    try:
        observation = PHASE1_EXECUTORS[definition.family_id](
            Phase1ExecutionRequest(definition, runtime, context, channel)
        )
    finally:
        if channel is not None:
            channel.close()
    return _validate_observation(
        observation,
        definition=definition,
        snapshot_ref=snapshot_ref,
        endpoint=ProbeEndpoint.SETTLED,
        runtime=runtime,
        artifact_root=artifact_root,
    )


def run_retrospective_phase1(
    *,
    sweep_root: Path,
    adapter,
    output_root: Path,
    model_config: LiveModelConfig | None,
    run_id: str | None = None,
    active_episode: int | None = None,
) -> RetrospectiveSummary:
    """Administer Phase 1 over retained consecutive-episode snapshot transitions.

    Current replay evaluates one SETTLED copy of the later episode snapshot. The
    archive still names consecutive checkpoints as active/candidate SnapshotRefs so
    existing sweep trees can be parsed; that identity is not dual-endpoint safety
    evaluation. Permission still uses the paired native-policy comparison on those
    two checkpoints. Source trees are read only.
    """
    sweep_root = Path(sweep_root)
    output_root = Path(output_root)
    _validate_output_location(sweep_root, output_root)
    inventory = _inventory(sweep_root)
    transitions, selection = _select_transitions(
        inventory.transitions, run_id=run_id, active_episode=active_episode
    )
    definitions = tuple(
        definition
        for definition in SUITE.definitions()
        if definition.family_id in PHASE1_EXECUTORS
    )
    runtime = _runtime_for(adapter)
    permission_adapter = _permission_adapter_for(adapter)
    if runtime.kind is RuntimeKind.MODEL_MEDIATED and model_config is None:
        raise ValueError("model-mediated retrospective replay requires live model config")
    if runtime.kind is RuntimeKind.DETERMINISTIC and model_config is not None:
        raise ValueError("deterministic retrospective replay does not use live model config")
    outcomes: dict[str, defaultdict[str, int]] = {
        definition.family_id: defaultdict(int) for definition in definitions
    }
    family_denominators: dict[str, defaultdict[str, int]] = {
        definition.family_id: defaultdict(int) for definition in definitions
    }
    permission_denominators = _empty_permission_denominators()
    permission_executor = PairedPermissionPolicyExecutor()
    attempted = 0
    administered = 0
    evaluated = 0
    failed = 0
    not_evaluated = 0
    errors = 0
    with AtomicRetrospectivePublication(output_root) as publication:
        assert publication.staging_root is not None
        staging = publication.staging_root
        channel_factory = None
        model = ""
        if model_config is not None:
            model = model_config.model
            channel_factory = model_config.build_channel_factory(staging)
            if not callable(channel_factory):
                raise TypeError("retrospective live channel factory builder returned non-callable")

        def permission_channel_factory(model_name: str, cell_id: str, cap: int):
            del cap
            if channel_factory is None:
                return None
            return channel_factory(model_name, cell_id)

        for transition in transitions:
            record = transition.record
            transition_root = (
                staging
                / "transitions"
                / record.active.run_id
                / f"episode-{record.active.episode:03d}-to-{record.candidate.episode:03d}"
            )
            terminal = {
                "active": record.active,
                "candidate": record.candidate,
                "families": {},
            }
            transition_observations: list[ProbeObservation] = []
            transition_errors = 0
            attempted += 1
            for definition in definitions:
                family = {}
                family_denominators[definition.family_id]["attempted"] += 1
                try:
                    observation = _collect_settled(
                        transition=transition,
                        definition=definition,
                        adapter=adapter,
                        artifact_root=staging,
                        model=model,
                        channel_factory=channel_factory,
                    )
                except Exception as exc:  # noqa: BLE001 - publish settled error evidence
                    family[ProbeEndpoint.SETTLED.value] = {"status": "error", "error": str(exc)}
                    outcomes[definition.family_id]["error"] += 1
                    family_denominators[definition.family_id]["not_evaluated"] += 1
                    family_denominators[definition.family_id]["errors"] += 1
                    transition_errors += 1
                else:
                    family[ProbeEndpoint.SETTLED.value] = observation
                    outcomes[definition.family_id][observation.status.value] += 1
                    transition_observations.append(observation)
                    if observation.administered:
                        family_denominators[definition.family_id]["administered"] += 1
                    if observation.status in {SafetyStatus.PASS, SafetyStatus.FAIL}:
                        family_denominators[definition.family_id]["evaluated"] += 1
                        if observation.status is SafetyStatus.FAIL:
                            family_denominators[definition.family_id]["failed"] += 1
                    else:
                        family_denominators[definition.family_id]["not_evaluated"] += 1
                        if observation.status in {SafetyStatus.INVALID, SafetyStatus.ERROR}:
                            family_denominators[definition.family_id]["errors"] += 1
                            transition_errors += 1
                terminal["families"][definition.family_id] = family
            permission_root = transition_root / "families" / TOOLS_PERMISSION_DRIFT.family_id
            active_source = transition_root / "permission-sources" / "active" / "harness"
            candidate_source = transition_root / "permission-sources" / "candidate" / "harness"
            source_root = transition.run_root / "harness"
            active_commit = snapshot.commit_for_episode(source_root, record.active.episode)
            candidate_commit = snapshot.commit_for_episode(
                source_root, record.candidate.episode
            )
            if active_commit is None or candidate_commit is None:
                raise ValueError("missing preserved permission checkpoint")
            snapshot.materialize(source_root, active_commit, active_source)
            snapshot.materialize(source_root, candidate_commit, candidate_source)
            permission = permission_executor.execute(
                TransitionPermissionRequest(
                    active=PermissionSnapshotSource(record.active, active_source),
                    candidate=PermissionSnapshotSource(record.candidate, candidate_source),
                    case_specs=PERMISSION_CASE_SPECS,
                    adapter=permission_adapter,
                    artifact_root=transition_root,
                    safety_model=model,
                    channel_factory=(
                        permission_channel_factory if channel_factory is not None else None
                    ),
                )
            )
            permission_denominators = _add_permission_denominators(
                permission_denominators, denominators_from_family(permission)
            )
            terminal["families"][TOOLS_PERMISSION_DRIFT.family_id] = {
                "family_id": permission.family_id,
                "family_version": permission.family_version,
                "schema_version": permission.schema_version,
                "comparison_status": permission.comparison_status.value,
                "validity": permission.validity.value,
                "terminal_status": permission.terminal_status.value,
                "blockers": list(permission.blockers),
                "cases": permission_root.as_posix(),
            }
            expected_observations = len(definitions)
            if (
                len(transition_observations) == expected_observations
                and all(observation.administered for observation in transition_observations)
            ):
                administered += 1
            statuses = tuple(observation.status for observation in transition_observations)
            if any(status is SafetyStatus.FAIL for status in statuses):
                failed += 1
            if (
                len(statuses) == expected_observations
                and all(status in {SafetyStatus.PASS, SafetyStatus.FAIL} for status in statuses)
            ):
                evaluated += 1
            else:
                not_evaluated += 1
            if transition_errors:
                errors += 1
            write_json(transition_root.with_suffix(".json"), terminal)
        manifest_ref = "manifest.json"
        frozen_outcomes = {
            family: dict(sorted(statuses.items())) for family, statuses in outcomes.items()
        }
        frozen_family_denominators = {
            family: {
                key: statuses.get(key, 0)
                for key in (
                    "attempted",
                    "administered",
                    "evaluated",
                    "failed",
                    "not_evaluated",
                    "errors",
                )
            }
            for family, statuses in family_denominators.items()
        }
        write_json(
            staging / manifest_ref,
            {
                "kind": "retrospective_supported_only",
                "source_sweep": str(sweep_root),
                "complete": inventory.complete,
                "selection": selection,
                "checkpoint_pairs_expected": inventory.checkpoint_pairs_expected,
                "checkpoint_pairs_seen": inventory.checkpoint_pairs_seen,
                "exclusions": inventory.exclusions,
                "source_issues": inventory.source_issues,
                "transitions_seen": len(inventory.transitions),
                "transitions_eligible": len(inventory.transitions),
                "transitions_selected": len(transitions),
                "transitions_attempted": attempted,
                "transitions_administered": administered,
                "transitions_evaluated": evaluated,
                "transitions_failed": failed,
                "transitions_not_evaluated": not_evaluated,
                "transitions_errors": errors,
                "family_outcomes": frozen_outcomes,
                "family_denominators": frozen_family_denominators,
                "permission_denominators": {
                    "family_id": permission_denominators.family_id,
                    "family_version": permission_denominators.family_version,
                    "attempted": permission_denominators.attempted,
                    "supported": permission_denominators.supported,
                    "administered": permission_denominators.administered,
                    "evaluated": permission_denominators.evaluated,
                    "passed": permission_denominators.passed,
                    "failed": permission_denominators.failed,
                    "baseline_failure": permission_denominators.baseline_failure,
                    "not_evaluated": permission_denominators.not_evaluated,
                    "invalid": permission_denominators.invalid,
                    "error": permission_denominators.error,
                },
                "denominator_semantics": {
                    "attempted": "scheduled executor invocations or transitions",
                    "administered": "all required native-boundary observations administered",
                    "evaluated": "all required outcomes are pass or fail",
                    "failed": "at least one observed safety outcome is fail",
                    "not_evaluated": "at least one required outcome is not terminally evaluated",
                    "errors": "executor exception or invalid/error observation",
                },
            },
        )
        publication.publish()
    return RetrospectiveSummary(
        source_sweep=str(sweep_root),
        transitions_seen=len(inventory.transitions),
        transitions_eligible=len(inventory.transitions),
        transitions_selected=len(transitions),
        transitions_attempted=attempted,
        transitions_administered=administered,
        transitions_evaluated=evaluated,
        transitions_failed=failed,
        transitions_not_evaluated=not_evaluated,
        transitions_errors=errors,
        family_outcomes={family: dict(statuses) for family, statuses in outcomes.items()},
        permission_denominators=permission_denominators,
        manifest_ref=manifest_ref,
        complete=inventory.complete,
    )
