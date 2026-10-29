"""Materialize and execute an adapted harness through the common evaluation layer."""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import ArtifactComponent, ArtifactRef, Outcome, PluginRef, StatePolicy, require
from proteus.core.methods import ActorFailed, ActorRequest
from proteus.core.serialization import Contract, canonical_json, content_hash, read_json
from proteus.evaluation.records import SubjectResult, Trace
from proteus.evaluation.runners import EnvironmentLease


FILE_TREE = PluginRef(name='proteus.file_tree', version='1')


def path_check(value):
    ArtifactComponent(path=value, content_hash=content_hash('path'), size_bytes=0)


@dataclass(frozen=True, kw_only=True)
class HarnessEnvironmentOptions(Contract):
    base: ArtifactRef | None = None
    mutable_paths: tuple[str, ...] = field(default=(), metadata={'uniqueItems': True})
    max_bytes: int = field(default=64 * 1024 * 1024, metadata={'minimum': 1})
    max_files: int = field(default=4096, metadata={'minimum': 1})

    def _validate(self):
        require(self.base is None or self.base.codec == FILE_TREE, 'harness base must be a file-tree artifact')
        require((self.base is not None) == bool(self.mutable_paths),
                'an overlay needs both a pinned base and explicit mutable paths')
        for name in self.mutable_paths:
            path_check(name)


def validate_environment(execution, options):
    env = execution.environment
    require(env.provider == 'host' and env.network == 'unrestricted',
            'harness materialization requires explicit trusted-host access; adapter isolation is separate')
    require(not env.resources and not env.allowed_hosts and not env.read_scopes and not env.write_scopes
            and env.image_digest is None and env.build_hash is None,
            'materialization does not enforce container/resource/network restrictions')
    require(execution.state_policy == StatePolicy.INDEPENDENT,
            'harness candidate trials currently require independent state')


class HarnessEnvironment:
    def __init__(self, options):
        self.options = options

    @contextmanager
    def open(self, artifact, store, root, execution):
        options = self.options
        # Codecs own canonical representations; the bridge owns materialized files.
        files = dict(store.files(artifact))
        if options.base is not None:
            require(set(files) <= set(options.mutable_paths), 'candidate writes outside declared harness overlay')
            base = store.files(options.base)
            materialized = {path: value for path, value in base.items() if path not in options.mutable_paths}
            materialized.update(files)
        else:
            require(artifact.codec == FILE_TREE, 'non-file harness candidate requires an explicit base projection')
            materialized = files
        require(len(materialized) <= options.max_files and sum(len(v.data) for v in materialized.values()) <= options.max_bytes,
                'materialized harness exceeds declared limits')
        effective = store.put(FILE_TREE, materialized)
        harness = store.materialize(effective, root / 'harness')
        yield EnvironmentLease(loaded_artifact_hash=artifact.content_hash, local_root=harness,
            handle={'root': root, 'effective': effective, 'base': options.base,
                    'projection_hash': content_hash(options)})


@dataclass(frozen=True, kw_only=True)
class HarnessSubjectOptions(Contract):
    output_file: str = 'notes/prediction.md'
    output_json: bool = False

    def _validate(self):
        path_check(self.output_file)


def validate_subject(recipe, options, registry):
    subject = recipe.subject
    require(subject.actor is not None, 'harness subject needs a configured target actor role')
    require(not subject.mutable_scopes and not subject.input_types,
            'harness subject currently uses independent files and no candidate-code input ports')
    require(recipe.execution is not None and recipe.execution.state_policy == StatePolicy.INDEPENDENT,
            'harness subject requires independent execution')
    env = registry.get(recipe.execution.environment.backend.plugin)
    require('environment.harness_tree' in env.capabilities, 'environment cannot materialize a harness candidate')
    actor = recipe.actors[subject.actor]
    plugin = registry.get(actor.runtime.plugin, 'actor')
    require({'actor.execute', 'harness.candidate_tree'} <= set(plugin.capabilities),
            'target role lacks harness.candidate_tree execution capability')
    require('actor.workspace' not in plugin.capabilities, 'target role must execute candidates, not a coding workspace')
    require(actor.session.value == 'fresh', 'harness trials require fresh target-role instances')
    require(recipe.execution.timeout_seconds is None or 'request.timeout' in plugin.capabilities,
            'harness role cannot enforce the declared per-request execution timeout')
    require(subject.initial_artifact.codec.name in ('proteus.file_tree', 'proteus.text_components'),
            'harness subject requires a supported candidate representation projection')
    environment_options = env.options.from_dict(recipe.execution.environment.backend.options)
    base = getattr(environment_options, 'base', None)
    require(base is None or base.runtime is None or base.runtime == actor.runtime.plugin,
            'harness base is pinned to a different runtime role')


class HarnessSubject:
    def __init__(self, options):
        self.options = options

    def run(self, context):
        options = self.options
        lease = context.lease
        require(context.role_spec is not None and context._role_runtime is not None,
                'harness subject has no configured runtime role')
        require(lease.handle is not None and lease.local_root == lease.handle['root'] / 'harness',
                'harness materialization provenance is missing')
        effective = lease.handle['effective']
        require(options.output_file not in {item.path for item in effective.components},
                'prediction output cannot overwrite candidate input')
        prompt = ('Execute this task using the loaded harness state. Write only the prediction to '
                  f'harness/{options.output_file} (relative to the run root), or to '
                  f'{options.output_file} relative to your harness directory. '
                  'Do not replace the task with self-improvement work.\nTask input:\n' + canonical_json(context.task.input))
        request = ActorRequest(prompt=prompt, timeout_seconds=context.execution.timeout_seconds)
        runtime = context._role_runtime
        require(hasattr(runtime, 'execute_candidate'), 'target runtime cannot load a prepared candidate')
        try:
            result = runtime.execute_candidate(context.role_spec, request, lease.handle['root'])
        except ActorFailed as exc:
            result = exc.result
        status, prediction = Outcome.SUCCEEDED, None
        diagnostic = result.diagnostic
        output = None
        try:
            files = ArtifactStore.read_tree(lease.local_root,
                max_bytes=sum(item.size_bytes for item in effective.components) + 8 * 1024 * 1024,
                max_files=len(effective.components) + 256)
            output = files.get(options.output_file)
        except Exception as exc:
            # Output validation failed after measured execution. Retain its usage
            # and receipt identity instead of converting the work to unknown/zero.
            status, diagnostic = Outcome.INFRA_ERROR, f'{type(exc).__name__}: {exc}'
        if result.status.value != 'completed':
            status = Outcome.INFRA_ERROR
        elif status == Outcome.INFRA_ERROR:
            pass
        elif output is None:
            status, diagnostic = Outcome.TASK_FAILED, 'harness did not produce the declared prediction file'
        else:
            try:
                text = output.data.decode('utf-8').strip()
                prediction = read_json(text) if options.output_json else text
            except (UnicodeError, ValueError, TypeError):
                status, diagnostic = Outcome.TASK_FAILED, 'harness prediction has invalid encoding/format'
        identity = {'candidate_artifact_hash': context.candidate.artifact.content_hash,
                    'effective_harness_hash': effective.content_hash,
                    'base_artifact_hash': lease.handle['base'].content_hash if lease.handle['base'] else None,
                    'projection_hash': lease.handle['projection_hash']}
        return SubjectResult(loaded_artifact_hash=lease.loaded_artifact_hash,
            runtime=replace(result.runtime, environment_hash=content_hash(context.execution.environment),
                            settings={**result.runtime.settings, 'harness_candidate': identity}),
            status=status, output=prediction, state=context.state, trace=Trace(events=result.trace),
            usage=result.usage, diagnostic=diagnostic)


def verify_subject_receipts(root, result):
    """Verify target-role measurements without constructing/running the adapter."""
    from proteus.execution.harness_usage import INFERENCE, collect
    from proteus.core.serialization import json_value
    marker = result.runtime.settings.get('native_usage')
    if marker is None:
        return []
    from proteus.core.native_inputs import input_directory
    directory = input_directory(root)
    require(all(not p.is_symlink() for p in (root, directory, directory.parent)),
            'harness subject evidence directory cannot be a symlink')
    require(marker.get('version') == 1, 'subject native usage must describe one act trial')
    usage, expected, records = collect(root, marker['backend'])
    require(expected is not None and json_value(marker) == expected, 'harness subject usage evidence differs')
    require(all(getattr(result.usage, key) == getattr(usage, key) for key in INFERENCE),
            'harness subject usage differs from native receipts')
    return records
