"""Prepare ONLY six fresh-H0 full studies with explicit effective concurrency.

No provider request, credential access, launch, old-run edit, or ledger copy.
The frozen API algorithm remains d0a9dc8. Root must independently accept before
any separately implemented paid launcher can execute these immutable profiles.
"""
from collections import Counter
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'sources/api'
sys.path.insert(0, str(SOURCE))
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import TaskDataset
from proteus.examples import combee_formula as module
from proteus.recipes.cli import installed_packages, source_pin
from proteus.recipes.manifest import SourcePin

EXPECTED_SOURCE = 'd0a9dc848e8978f14fac2e70e57a22998fdf07f5'
VARIANTS = ('naive_parallel', 'combee_fixed', 'combee_dynamic')
AUDIT = ROOT / 'combee-parallel-v3-1-prepared-profiles-audit.json'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, indent=2) + '\n'
    with path.open('x') as output:
        output.write(encoded)


def normalized(recipe):
    value = json.loads(json.dumps(recipe.to_dict()))
    value['budget']['max_concurrency'] = '<EXPLICIT_EFFECTIVE_CAP>'
    for actor in value['actors'].values():
        actor['budget']['max_concurrency'] = '<EXPLICIT_ROLE_CAP>'
        actor['runtime']['options']['base_url'] = '<SCOPED_ROUTE>'
        actor['runtime']['options']['dispatch_guard_path'] = '<SCOPED_GUARD>'
    for evaluation in value['evaluations']:
        evaluation['tasks']['suite']['options']['path'] = '<STUDY_TASKS>'
    return value


def main():
    os.umask(0o077)
    assert not AUDIT.exists(), 'Immutable preparation must not overwrite prior evidence'
    source = source_pin('proteus', SOURCE)
    assert source.revision == EXPECTED_SOURCE and source.dirty_patch_hash is None
    helper = SourcePin(name='linux-combee-explicit-concurrency-profile-v3-1',
        repository='local-content-addressed:' + Path(__file__).name,
        revision=sha(Path(__file__)))
    prepared = []
    for mode in ('offline', 'online'):
        for variant in VARIANTS:
            old_label = f'linux-combee-formula-{mode}-{variant}-v2'
            label = f'linux-combee-formula-{mode}-{variant}-v3-1'
            old, new = ROOT / 'formal' / old_label, ROOT / 'formal' / label
            assert not new.exists()
            old_study, data, _, old_runs = module.load(old)
            assert len(old_runs) == 1
            _, old_recipe, _ = old_runs[0]
            protected = {str(path): sha(path) for path in (
                old / 'study.json', old / 'tasks.json', old / 'provenance.json',
                *old.glob('recipes/*.json'), *old.glob('locks/*.json'))}
            settings = old_study.settings
            assert settings.seeds == (0,) and settings.modes == (mode,)
            assert settings.variants == (variant,)
            assert settings.max_workers == 8 and settings.batch_size == 50
            assert settings.max_num_rounds == 3 and settings.eval_steps == 500
            assert settings.online_eval_frequency == 15
            assert dict(Counter(row.task.split for row in data.records)) == {
                'train': 500, 'validation': 300, 'test': 200}
            assert old_recipe.budget.max_concurrency == 1
            guard_path = ROOT / 'guards' / (label + '.json')
            guard = {'version': 2,
                'admission_url': 'http://127.0.0.1:62125/admission/' + label,
                'token_env': 'PROTEUS_RELAY_TOKEN'}
            once(guard_path, guard)
            roles = {}
            for name, actor in old_recipe.actors.items():
                assert actor.budget.max_concurrency == 1
                assert actor.model == 'deepseek-flash'
                assert actor.model_settings['max_tokens'] == 4096
                options = dict(actor.runtime.options)
                options.update(base_url='http://127.0.0.1:62125/' + label + '/v1',
                    dispatch_guard_path=str(guard_path))
                roles[name] = replace(actor, budget=replace(actor.budget, max_concurrency=8),
                    runtime=replace(actor.runtime, options=options))
            settings = replace(settings, **roles,
                budget=replace(old_recipe.budget, max_concurrency=8))
            provenance = json.loads((old / 'provenance.json').read_text())
            provenance['Linux_profile'] = {**provenance['Linux_profile'],
                'id': label, 'source': source.to_dict(), 'admission_guard': guard}
            provenance['effective_concurrency_correction'] = {
                'old_profile': str(old),
                'old_attempt_status': 'Admission-held: actually serialized, NOT a valid parallel result',
                'old_spending_responses_and_unknown_reserves_retained': True,
                'initialization': 'Original empty H0, not a learned/failed candidate',
                'allowed_changes': ['run concurrency 1->8',
                    'generator/reflector/curator role concurrency 1->8',
                    'fresh root/scoped transport paths/lock provenance'],
                'algorithm_model_data_seed_quality_controls_unchanged': True,
                'reason': 'workflow=min(method8,run1); shared role budget1 also serialized child actor calls; curator proposal waves use stage-map',
                'not_published_speedup_replication': True,
                'not_an_independent_second_seed': True}
            module.prepare(new, data, settings, sources=[source, helper],
                packages=installed_packages(), provenance=provenance)
            _, loaded_data, _, runs = module.load(new)
            assert len(runs) == 1 and content_hash(loaded_data) == old_study.dataset_hash
            entry, recipe, lock = runs[0]
            assert normalized(recipe) == normalized(old_recipe)
            assert recipe.subject.initial_artifact == old_recipe.subject.initial_artifact
            assert recipe.budget.max_concurrency == 8
            assert all(actor.budget.max_concurrency == 8 for actor in recipe.actors.values())
            assert recipe.execution.max_workers == recipe.method.options['max_workers'] == 8
            assert not (new / 'runs').exists()
            assert all(sha(Path(path)) == digest for path, digest in protected.items())
            files = [new / 'study.json', new / 'tasks.json', new / 'provenance.json',
                new / 'recipes' / (entry.name + '.json'),
                new / 'locks' / (entry.name + '.json'), guard_path]
            record = {'label': label, 'root': str(new),
                'status': 'prepared_not_dispatched_pending_root_acceptance',
                'source_sha': source.revision, 'dataset_hash': content_hash(data),
                'config_hash': recipe.config_hash, 'lock_hash': content_hash(lock),
                'initial_H0': recipe.subject.initial_artifact.to_dict(),
                'budget': recipe.budget.to_dict(),
                'role_budgets': {name: actor.budget.to_dict() for name, actor in recipe.actors.items()},
                'method_options': recipe.to_dict()['method']['options'],
                'normalized_recipe_identical_except_declared_concurrency_and_paths': True,
                'previous_profile': old_label, 'previous_protected_hashes': protected,
                'files': [{'path': str(path), 'sha256': sha(path)} for path in files]}
            prepared.append(record)
            once(ROOT / 'handles' / (label + '.json'), {**record, 'pid': None})
    assert source_pin('proteus', SOURCE) == source
    audit = {'status': 'prepared_not_dispatched_pending_root_acceptance',
        'source_sha': source.revision, 'launcher_sha256': sha(Path(__file__)),
        'profiles': prepared, 'paid_calls_by_preparation': 0,
        'provider_key_or_original_ledger_access': False,
        'old_runs_serial_ACE_and_other_families_not_mutated': True,
        'global_authority_cap_or_gateway_concurrency_changed': False,
        'launch_function_intentionally_absent': True,
        'qualification_required': 'qualified-combee-effective-parallel-v3-1.json',
        'not_a_full_result': True}
    once(AUDIT, audit)
    print(json.dumps({'status': audit['status'], 'profiles': len(prepared),
        'audit': str(AUDIT), 'audit_sha256': sha(AUDIT),
        'launcher_sha256': sha(Path(__file__))}), flush=True)


if __name__ == '__main__':
    main()

