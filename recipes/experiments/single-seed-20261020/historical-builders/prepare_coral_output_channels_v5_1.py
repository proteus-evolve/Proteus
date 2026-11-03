"""Prepare two FULL fresh original-H0 CORAL profiles; never dispatch a model."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'sources/coral-output-channels-v5_1'
sys.path.insert(0, str(SOURCE))
from proteus.benchmarks.coral_circle_packing.recipe import prepare, register
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import TaskDataset
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.cli import installed_packages, source_pin
from proteus.recipes.config import RecipeConfig, load_recipe
from proteus.recipes.manifest import SourcePin

SOURCE_SHA = '7ca7742d18f267b5a2805dad59979e328d3a8f36'
GATE = ROOT / 'qualified-coral-output-channels-v5-1.json'
AUDIT = ROOT / 'coral-v5-1-prepared-profiles-audit.json'
SDK = ROOT / 'harbor-venv/lib/python3.12/site-packages/harbor/environments/docker/docker.py'
WORKER_SHA = '2ab9ba3e043c19967b2218bb614b64ae0bc9c81ce892c77c6517005dd309e031'
FEEDBACK_SHA = '2df07d4d6ab5169f16f7e51385ba29278ba9edfff149e102d9b9e8590c6e3250'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, indent=2) + '\n'
    with path.open('x') as output:
        output.write(encoded)


def transform(value, substitutions):
    if isinstance(value, str):
        for before, after in substitutions:
            value = value.replace(before, after)
        return value
    if isinstance(value, list): return [transform(item, substitutions) for item in value]
    if isinstance(value, dict): return {key: transform(item, substitutions) for key, item in value.items()}
    return value


def differences(before, after, path=''):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            result += differences(before.get(key), after.get(key), path + '/' + key)
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for index in range(max(len(before), len(after))):
            result += differences(before[index] if index < len(before) else None,
                after[index] if index < len(after) else None, path + '/' + str(index))
        return result
    return [{'path': path, 'before': before, 'after': after}]


def main():
    os.umask(0o077)
    assert not AUDIT.exists()
    gate = json.loads(GATE.read_text())
    assert gate['status'] == 'passed_actual_unpaid_Harbor_channel_separation_and_unavailable_score_gates'
    assert gate['source_sha'] == SOURCE_SHA and gate['paid_provider_requests'] == 0
    assert gate['helper_sha256'] == sha(ROOT / 'qualify_coral_output_channels_v5_1.py')
    assert gate['installed_Harbor_SDK_sha256_before_after_unchanged'] == sha(SDK)
    assert gate['actual_subject_trials'] == 9
    assert sha(ROOT / 'circle_linux_worker_v4_1.py') == WORKER_SHA
    assert sha(ROOT / 'circle_released_feedback_v4_1.py') == FEEDBACK_SHA
    source = source_pin('proteus', SOURCE)
    assert source.revision == SOURCE_SHA and source.dirty_patch_hash is None
    old_source = source_pin('proteus', ROOT / 'sources/coral')
    assert old_source.revision == '722ac71a36ab355f6512afb3214f05722eb90b9b' and old_source.dirty_patch_hash is None
    helper = SourcePin(name='full-coral-channel-profiles-v5-1',
        repository='local-content-addressed:' + Path(__file__).name, revision=sha(Path(__file__)))
    profiles = []
    for workers in (1, 2):
        old_label, label = f'linux-coral-circle-{workers}-workers-v4-1', f'linux-coral-circle-{workers}-workers-v5-1'
        old, new = ROOT / 'formal' / old_label, ROOT / 'formal' / label
        assert not new.exists()
        protected_paths = [old / 'recipe.json', old / 'lock.json', old / 'tasks.json', old / 'full-protocol.json']
        protected = {str(path): sha(path) for path in protected_paths}
        old_recipe = load_recipe(old / 'recipe.json')
        old_data = TaskDataset.from_json((old / 'tasks.json').read_text())
        base_path = prepare(new, str(ROOT / 'harbor-venv/bin/python'))
        base = load_recipe(base_path)
        data = TaskDataset.from_json((new / 'tasks.json').read_text())
        assert base.subject.initial_artifact == old_recipe.subject.initial_artifact
        assert base.subject.initial_artifact.content_hash == 'sha256:0991f8ff72215ee85f77c94e331066b1647ff2d264a97a6a7730f2c6f6241a40'
        raw = transform(old_recipe.to_dict(), [(str(old), str(new)), (old_label, label),
            (content_hash(old_data), content_hash(data))])
        raw['execution']['environment']['backend']['options']['runtime_hash'] = base.execution.environment.backend.options['runtime_hash']
        raw['reference']['deviations'].append(
            'New isolated framework source7ca7742 captures original candidate stdout/stderr in separate files, retains raw transport diagnostics and implementation/argv witnesses '
            'and preserves formal null for non-success rollouts while validating raw backend evidence. '
            'It supersedes previous core-source identity annotations, not the model/controller/geometry/budget recipe. '
            'No old candidate, model reply, failure history or private score warm start; original H0 and full20proposals perarm.')
        registry = builtin_registry(); register(registry)
        recipe = registry.resolve(RecipeConfig.from_dict(raw))
        assert recipe.method.options == old_recipe.method.options
        assert recipe.method.options['max_submissions'] == 20
        assert recipe.execution.timeout_seconds == 600
        assert dict(recipe.execution.environment.resources) == {'cpus': 1, 'memory_mb': 2048}
        assert recipe.execution.environment.backend.options['source_build'] == old_recipe.execution.environment.backend.options['source_build']
        assert recipe.seed == old_recipe.seed == 0 and len(recipe.workers) == workers
        assert recipe.budget == old_recipe.budget and recipe.subject == old_recipe.subject
        assert all(worker.runtime.options['command'][2] == str(20 // workers) for worker in recipe.workers)
        for worker in recipe.workers:
            assert worker.runtime.options['command'][4] == label
            for path, expected in worker.runtime.options['command_files'].items():
                assert 'sha256:' + sha(Path(path)) == expected
        def normalized_config(item, root, ident, dataset_hash):
            value = transform(item.to_dict(), [(str(root), '<STUDY>'), (ident, '<PROFILE>'),
                (dataset_hash, '<DATASET_HASH>')])
            value['execution']['environment']['backend']['options']['runtime_hash'] = '<TRANSPORT_RUNTIME>'
            return value
        before = normalized_config(old_recipe, old, old_label, content_hash(old_data))
        after = normalized_config(recipe, new, label, content_hash(data))
        assert after['reference']['deviations'][:-1] == before['reference']['deviations']
        after['reference']['deviations'] = before['reference']['deviations']
        assert after == before, 'Undeclared full-science configuration difference'
        assert transform(old_data.to_dict(), [(str(old), '<STUDY>')]) == transform(data.to_dict(), [(str(new), '<STUDY>')])
        h0_hashes = {}
        for component in recipe.subject.initial_artifact.components:
            digest = component.content_hash.removeprefix('sha256:')
            old_blob = old / 'artifact-store/blobs' / digest
            new_blob = new / 'artifact-store/blobs' / digest
            assert old_blob.read_bytes() == new_blob.read_bytes()
            assert sha(old_blob) == sha(new_blob) == digest
            h0_hashes[component.path] = digest
        assert set(h0_hashes) == {'RESEARCH.md', 'initial_program.py', 'main.py', 'research.py'}
        task_hashes = {}
        for relative in ('instruction.md', 'task.toml', 'tests/verify.py', 'tests/test.sh'):
            assert (old / 'task' / relative).read_bytes() == (new / 'task' / relative).read_bytes()
            task_hashes[relative] = sha(new / 'task' / relative)
        lock = registry.make_lock(recipe, [source, helper], packages=installed_packages())
        # This is new unpublished preparation, not rewriting an active recipe.
        (new / 'recipe.json').write_text(recipe.to_json() + '\n')
        once(new / 'lock.json', lock.to_dict())
        full = {'label': label, 'source_sha': source.revision, 'original_source_sha': old_source.revision,
            'workers': workers, 'total_model_proposals': 20, 'per_worker_proposals': 20 // workers,
            'seed': 0, 'same_original_H0': True, 'same_original_H0_file_bytes': h0_hashes,
            'same_original_task_bytes': task_hashes,
            'full_normalized_scientific_recipe_equals_old_except_runtime_identity_and_explicit_infra_deviation': True,
            'normalized_scientific_comparison': {'old_label': old_label, 'new_label': label,
                'complete_raw_config_differences': differences(old_recipe.to_dict(), recipe.to_dict()),
                'only_allowed_semantic_change': 'new transport runtime identity plus one explicit infrastructure deviation',
                'normalized_scientific_differences': differences(before, after)},
            'complete_method_options_preserved': True, 'same_v4_1_model_output_instructions_worker_and_feedback': True,
            'worker_sha256': WORKER_SHA, 'feedback_helper_sha256': FEEDBACK_SHA,
            'old_failed_replies_candidates_costs_nulls_not_warm_started_or_changed': True,
            'framework_source_fixes_only': ['capture original candidate stdout/stderr separately before SDK merging; retain raw diagnostics and implementation/argv witnesses', 'do not promote backend rewards to formal scores for non-success rollout'],
            'gate_sha256': sha(GATE), 'preparation_helper_sha256': sha(Path(__file__)),
            'source': source.to_dict(), 'config_hash': recipe.config_hash, 'lock_hash': content_hash(lock),
            'initial_H0': recipe.subject.initial_artifact.to_dict(),
            'model': 'deepseek-flash', 'max_tokens': 8192, 'temperature': 0,
            'calibration_calls_required': 0, 'final_confirmation_same_task_not_generalization': True,
            'model_calls_or_private_selection_by_preparation': 0}
        once(new / 'full-protocol.json', full)
        assert not (new / 'run').exists() and all(sha(Path(path)) == digest for path, digest in protected.items())
        files = [new / name for name in ('recipe.json', 'lock.json', 'tasks.json', 'full-protocol.json')]
        record = {**full, 'root': str(new), 'status': 'prepared_not_dispatched_pending_root_release',
            'files': [{'path': str(path), 'sha256': sha(path)} for path in files],
            'protected_old_config_hashes_unchanged': protected}
        profiles.append(record)
        once(ROOT / 'handles' / (label + '.json'), {'label': label, 'root': str(new),
            'pid': None, 'status': 'prepared_not_dispatched', 'source_sha': source.revision, 'workers': workers})
    assert source_pin('proteus', ROOT / 'sources/coral') == old_source
    receipt = {'status': 'two_full_original_H0_profiles_prepared_not_paid_released',
        'source_sha': source.revision, 'helper_sha256': sha(Path(__file__)),
        'runtime_gate_sha256': sha(GATE), 'installed_Harbor_SDK_unchanged_sha256': sha(SDK),
        'profiles': profiles, 'paid_provider_calls': 0, 'no_new_model_calibration': True,
        'old_source_live_runs_and_original_accounting_unchanged': True,
        'required': 'Independent root approval and exact single-label admission before a separate one-shot launcher'}
    once(AUDIT, receipt)
    print(json.dumps({'status': receipt['status'], 'profiles': len(profiles),
        'audit_sha256': sha(AUDIT), 'helper_sha256': receipt['helper_sha256']}), flush=True)


if __name__ == '__main__':
    main()
