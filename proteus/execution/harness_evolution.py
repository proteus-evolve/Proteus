"""A managed Native episode: immutable active input, editable pending output.

The shared workspace service owns input verification and artifact publication. This
adapter never receives study-private evaluations, and never seeds over a candidate.
"""

import shutil
import time
from dataclasses import asdict, replace

from proteus.artifacts.codecs import components
from proteus.artifacts.store import ArtifactStore
from proteus.core.adapter import EpisodeSpec
from proteus.core.contracts import OperationStatus, RuntimeIdentity, Usage, require
from proteus.core.methods import ActorFailed, ActorResult
from proteus.core.native_inputs import read_native_inputs
from proteus.core.serialization import content_hash
from proteus.execution.workspace_records import WorkspaceInput


def execute_episode(runtime, spec, request, workspace):
    require(spec.session.value == 'fresh' and request.session_id is None and request.handoff is None,
            'managed evolution episodes require fresh outer role instances')
    inputs = request.workspace
    require(inputs is not None and inputs.episode is not None and inputs.active_seed is not None,
            'evolution actor requires explicit working, active and episode inputs')
    require(not request.system, 'evolution prompts are declared in the episode control')
    require(not read_native_inputs(workspace), 'evolution actor receipt identity was already dispatched')
    control, adapter = inputs.episode, runtime.adapter(spec)
    staged = bool(getattr(adapter, 'staged_activation', False))
    require(staged == ('harness.staged_activation' in runtime.integration.capabilities),
            'adapter staged activation differs from its capability declaration')
    harness, active = workspace / 'harness', workspace / 'active'
    shutil.copytree(workspace / 'task/input', harness)
    shutil.copytree(workspace / 'task/active', active)
    prepare = getattr(adapter, 'prepare_candidate', None)
    if prepare is not None:
        prepare(harness)
        prepare(active)
    validate = getattr(adapter, 'validate_candidate', None)
    if validate is not None:
        require(not validate(active), 'active harness failed model-free viability')
    started = time.monotonic()
    result, trace, failure = None, (), None
    try:
        result = adapter.run_episode(EpisodeSpec(root=workspace, episode=control.number,
            model=spec.model or '', phase_prompts=control.prompts, phases=control.phases,
            max_turns=control.max_turns, min_turns_per_phase=control.min_turns_per_phase,
            phase_turns=control.phase_turns, hard_max_turns=control.hard_max_turns,
            checkpoint_turns=control.checkpoint_turns, announce_budget=control.announce_budget,
            seed=control.seed, active_root=active if staged else None,
            continuity_mode=getattr(adapter, 'continuity_mode', 'native'),
            readonly_task_paths=('input', 'active', 'history')))
        trace = tuple(asdict(event) for event in adapter.read_trace(workspace, control.number))
    except Exception as exc:
        from proteus.core.recovery import RecoveryRequired
        if isinstance(exc, RecoveryRequired):
            raise
        failure = exc
    from proteus.execution.harness_usage import collect
    usage, marker, _ = collect(workspace, runtime.name, episode=control.number, phases=control.phases)
    if usage is None:
        counters = result.counters if result else {}
        usage = Usage(input_tokens=counters.get('tokens_in'), output_tokens=counters.get('tokens_out'))
    settings = {'requested_model': spec.model, 'harness': runtime.name,
                'model_selection': runtime.model_selection(spec), 'episode': control.to_dict(),
                'active_artifact_hash': inputs.active_seed.content_hash,
                'working_artifact_hash': inputs.seed.content_hash}
    if marker is not None:
        settings['native_usage'] = marker
    success = failure is None and result.ok
    if success:
        # A validator may read/build candidate code, but must not mutate its files.
        try:
            files = ArtifactStore.read_tree(harness, max_bytes=inputs.max_output_bytes, max_files=inputs.max_output_files)
            reason = validate(harness) if validate is not None else ''
            require(isinstance(reason, str), 'candidate validator must return a diagnostic string')
            require(files == ArtifactStore.read_tree(harness, max_bytes=inputs.max_output_bytes,
                        max_files=inputs.max_output_files), 'candidate validator changed the candidate')
            settings['candidate_viability'] = {'valid': not bool(reason), 'diagnostic': reason,
                                              'tree_hash': content_hash(components(files))}
            shutil.copytree(harness, workspace / 'task/output', dirs_exist_ok=True)
        except Exception as exc:
            from proteus.core.recovery import RecoveryRequired
            if isinstance(exc, RecoveryRequired):
                raise
            success, failure = False, exc
    from proteus.core.continuity import _redact
    outcome = ActorResult(text='\n'.join(event['text'] for event in trace if event.get('text')),
        trace=trace, runtime=RuntimeIdentity(runtime=spec.runtime.plugin, settings=settings),
        usage=replace(usage, wall_seconds=time.monotonic() - started),
        status=OperationStatus.COMPLETED if success else OperationStatus.FAILED,
        diagnostic=_redact(f'{type(failure).__name__}: {failure}') if failure else result.error)
    if failure is not None:
        raise ActorFailed(outcome) from failure
    return outcome


class HarnessEpisodeStage:
    def __init__(self, options):
        self.options = options

    def run(self, context, inputs):
        request = WorkspaceInput(seed=inputs['seed'], active_seed=inputs['active'],
            episode=inputs['episode'], history=inputs['history'].ref(),
            max_output_bytes=self.options.max_output_bytes, max_output_files=self.options.max_output_files)
        result = context.actor('Execute the declared self-evolution episode.', workspace=request)
        require(result.status == OperationStatus.COMPLETED, result.diagnostic or 'evolution episode failed')
        return {'response': result}
