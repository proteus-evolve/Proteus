"""Separately approved FULL fresh-H0 ACE online study, not failed-run repair.

Old933successes/502unknown/profile remain intact. Same science/seed/data/roles;
only study-root and scoped transport identities/pins change. No paid execution
until explicit root acceptance; no resume, filtering, imputation or paid retry.
"""
import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'sources/api'
sys.path.insert(0, str(SOURCE))
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import TaskDataset
from proteus.examples import ace_formula as module
from proteus.recipes.cli import source_pin, installed_packages
from proteus.recipes.config import load_recipe
from proteus.recipes.manifest import SourcePin

LABEL = 'linux-ace-formula-online-v3'
OLD_LABEL = 'linux-ace-formula-online-v2'
OLD = ROOT / 'formal' / OLD_LABEL
NEW = ROOT / 'formal' / LABEL
EXPECTED_SOURCE = 'd0a9dc848e8978f14fac2e70e57a22998fdf07f5'
GUARD = ROOT / 'guards' / (LABEL + '.json')
AUDIT = ROOT / 'ace-online-v3-prepared-profile-audit.json'
ACCEPTED = ROOT / 'ACE-online-v3-release-accepted.json'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as output:
        output.write(json.dumps(value, indent=2) + '\n')


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.pending')
    temporary.write_text(json.dumps(value, indent=2) + '\n'); temporary.replace(path)


def normalized_recipe(raw):
    value = json.loads(json.dumps(raw))
    for role in value['actors'].values():
        options = role['runtime']['options']
        options['base_url'] = '<SCOPED_ROUTE>'
        options['dispatch_guard_path'] = '<SCOPED_GUARD_PATH>'
    for evaluation in value['evaluations']:
        evaluation['tasks']['suite']['options']['path'] = '<STUDY_TASKS_PATH>'
    return value


def source_identity():
    source = source_pin('proteus', SOURCE)
    assert source.revision == EXPECTED_SOURCE and source.dirty_patch_hash is None
    return source


def prepare():
    assert not NEW.exists() and not AUDIT.exists()
    assert json.loads((ROOT / 'handles' / (OLD_LABEL + '.json')).read_text())['status'] == 'failed'
    recovery_path = ROOT / 'ace-online-v2-recovery-audit.json'
    recovery = json.loads(recovery_path.read_text())
    assert recovery['terminal_result_status'] == 'failed' and not recovery['pending_operation_requests']
    protected = {item['path']: item['sha256'] for item in recovery['protected_files']}
    assert all(sha(Path(path)) == expected for path, expected in protected.items())
    old_study = module.Study.from_json((OLD / 'study.json').read_text())
    old_settings = old_study.settings
    assert old_settings.seeds == (0,) and old_settings.modes == ('online',)
    assert old_settings.max_num_rounds == 3 and old_settings.eval_steps == 100 and old_settings.online_eval_frequency == 15
    guard = {'version': 2, 'admission_url': 'http://127.0.0.1:62125/admission/' + LABEL,
        'token_env': 'PROTEUS_RELAY_TOKEN'}
    once(GUARD, guard)
    roles = {}
    for name in ('generator', 'reflector', 'curator'):
        actor = getattr(old_settings, name)
        assert actor.model == 'deepseek-flash' and actor.model_settings['max_tokens'] == 4096
        options = dict(actor.runtime.options)
        options.update(base_url='http://127.0.0.1:62125/' + LABEL + '/v1', dispatch_guard_path=str(GUARD))
        roles[name] = replace(actor, runtime=replace(actor.runtime, options=options))
    settings = replace(old_settings, **roles)
    data = TaskDataset.from_json((OLD / 'tasks.json').read_text())
    assert content_hash(data) == old_study.dataset_hash
    assert dict(Counter(item.task.split for item in data.records)) == {'train': 500, 'validation': 300, 'test': 200}
    source = source_identity()
    provenance = json.loads((OLD / 'provenance.json').read_text())
    provenance['Linux_profile'] = {'id': LABEL, 'host': 'i-0faa491e03aae7dbd',
        'source': source.to_dict(), 'data_hash': content_hash(data), 'admission_guard': guard,
        'shared_budget_authority': 'Original physical Mac api-budget.sqlite, no reset or clone',
        'shared_cap_USD': 200, 'warning_USD': 150, 'AIME_global_cap_USD': 50}
    provenance['separate_fresh_research_attempt'] = {
        'previous_profile': str(OLD), 'previous_result': 'Failed at138/200online stream after933successful paid calls and1HTTP502 with unknown reserve; retained, not reported as success.',
        'reason': 'No saved successful response; official journal terminal failed. A new full study, not checkpoint rescue or a paid retry of the old identity.',
        'initialization': 'Empty original ACE playbook H0 only, never prior candidate-000233 or earlier learning',
        'scientific_changes': False, 'new_transport_and_study_identity_only': True,
        'same_dataset_all500_300_200': True, 'full_online_stream': 200,
        'online_semantics': 'Fresh H0 baseline200, then14prequential windows (15each, last5) over200items, followed by observed learning under unchanged three-round ACE recipe. Not a hidden held-out transfer estimate.',
        'one_seed_repeated_attempt_not_two_independent_replicates': True,
        'prior_spend_unknown_reserve_and_failed_branch_retained': True}
    helper = SourcePin(name='linux-ace-online-fresh-launcher-v3',
        repository='local-content-addressed:' + Path(__file__).name, revision=sha(Path(__file__)))
    module.prepare(NEW, data, settings, sources=[source, helper], packages=installed_packages(), provenance=provenance)
    study, loaded_data, registry, runs = module.load(NEW)
    assert len(runs) == 1 and content_hash(loaded_data) == old_study.dataset_hash
    entry, recipe, lock = runs[0]
    old_recipe = load_recipe(OLD / 'recipes/seed-0-online.json')
    assert normalized_recipe(recipe.to_dict()) == normalized_recipe(old_recipe.to_dict())
    assert recipe.subject.initial_artifact == old_recipe.subject.initial_artifact
    assert len(next(item for item in recipe.evaluations if item.id == 'stream').tasks.task_ids) == 200
    assert not (NEW / 'runs').exists()
    assert all(sha(Path(path)) == expected for path, expected in protected.items())
    handle = {'label': LABEL, 'root': str(NEW), 'pid': os.getpid(), 'status': 'prepared_not_dispatched',
        'source_sha': source.revision, 'dataset_hash': content_hash(data),
        'full_splits': {'train': 500, 'validation': 300, 'test': 200},
        'separate_fresh_attempt_after_failed_profile': OLD_LABEL}
    once(ROOT / 'handles' / (LABEL + '.json'), handle)
    files = [NEW / 'study.json', NEW / 'recipes/seed-0-online.json', NEW / 'locks/seed-0-online.json',
        NEW / 'tasks.json', NEW / 'provenance.json', GUARD]
    audit = {'status': 'prepared_not_dispatched_pending_root_acceptance', 'label': LABEL,
        'root': str(NEW), 'source_sha': source.revision, 'launcher_sha256': sha(Path(__file__)),
        'config_hash': recipe.config_hash, 'lock_hash': content_hash(lock), 'dataset_hash': content_hash(data),
        'initial_artifact': recipe.subject.initial_artifact.to_dict(),
        'same_original_empty_H0': True, 'no_old_candidate_or_history_warm_start': True,
        'normalized_complete_recipe_equals_failed_profile': True,
        'allowed_normalization': ['base_url new scoped route', 'dispatch_guard_path', 'study taskfile path'],
        'unchanged_method_options': dict(recipe.method.options), 'unchanged_budget': recipe.budget.to_dict(),
        'complete_online_stream': 200, 'same_H0_baseline_predictions': 200,
        'source_dataset_split_counts': {'train': 500, 'validation': 300, 'test': 200},
        'actual_new_root_file_hashes': [{'path': str(path), 'sha256': sha(path), 'bytes': path.stat().st_size} for path in files],
        'previous_protected_file_hashes_unchanged': protected,
        'old933successes_502unknown_and_failed_profile_preserved': True,
        'no_new_provider_call_or_ledger_copy': True, 'no_instance_or_cap_or_concurrency_change': True,
        'readiness_not_completion': True, 'required_root_acceptance_path': str(ACCEPTED),
        'required_root_acceptance_fields': {'accepted': True, 'launcher_sha256': sha(Path(__file__)),
            'prepared_audit_sha256': 'SHA256 of this exact receipt after write'},
        'approved_launch_command': 'venv/bin/python run_ace_online_fresh_v3.py --launch-once'}
    once(AUDIT, audit)
    print(json.dumps({'status': audit['status'], 'label': LABEL, 'audit': str(AUDIT), 'audit_sha256': sha(AUDIT),
        'launcher_sha256': sha(Path(__file__)), 'config_hash': recipe.config_hash, 'lock_hash': content_hash(lock),
        'dataset_hash': content_hash(data), 'unchanged_budget': audit['unchanged_budget'],
        'initial_H0': recipe.subject.initial_artifact.content_hash}), flush=True)


def check_accepted():
    audit = json.loads(AUDIT.read_text()); accepted = json.loads(ACCEPTED.read_text())
    assert accepted.get('accepted') is True and accepted.get('launcher_sha256') == sha(Path(__file__)) == audit['launcher_sha256']
    assert accepted.get('prepared_audit_sha256') == sha(AUDIT)
    assert all(sha(Path(item['path'])) == item['sha256'] for item in audit['actual_new_root_file_hashes'])
    source_identity()
    return audit


def launch_once():
    audit = check_accepted()
    intent = ROOT / 'ace-online-v3-launch-intent.json'
    assert not intent.exists() and not (ROOT / 'ace-online-v3-launch.json').exists()
    handle = json.loads((ROOT / 'handles' / (LABEL + '.json')).read_text())
    assert handle['status'] == 'prepared_not_dispatched' and not (NEW / 'runs').exists()
    token = (ROOT / 'private-bridge/scoped-token.txt').read_text().strip()
    request = urllib.request.Request('http://127.0.0.1:62125/admission/' + LABEL, headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert json.load(response).get('allowed') is True
    once(intent, {'started_epoch': time.time(), 'label': LABEL, 'launcher_sha256': sha(Path(__file__)), 'retry': False})
    log = ROOT / 'logs' / (LABEL + '.log')
    with log.open('xb') as output:
        process = subprocess.Popen([sys.executable, '-u', str(Path(__file__)), '--execute-prepared'], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    record = {'status': 'launched_not_full_result', 'label': LABEL, 'pid': process.pid, 'started_epoch': time.time(),
        'launcher_sha256': sha(Path(__file__)), 'prepared_audit_sha256': sha(AUDIT),
        'source_sha': EXPECTED_SOURCE, 'config_hash': audit['config_hash'], 'lock_hash': audit['lock_hash'],
        'initialization': 'Original empty H0, entire200stream, not old run recovery', 'log': str(log),
        'prior_failed_profile_and_spending_retained': True, 'no_paid_retry_or_warm_start': True}
    once(ROOT / 'ace-online-v3-launch.json', record)
    print(json.dumps(record), flush=True)


def execute():
    check_accepted()
    handle_path = ROOT / 'handles' / (LABEL + '.json')
    prior = json.loads(handle_path.read_text())
    assert prior['status'] == 'prepared_not_dispatched'
    assert not (NEW / 'runs').exists(), 'Never auto-resume a dispatched study'
    os.environ['PROTEUS_RELAY_TOKEN'] = (ROOT / 'private-bridge/scoped-token.txt').read_text().strip()
    save(handle_path, {**prior, 'pid': os.getpid(), 'status': 'running', 'started_epoch': time.time()})
    try:
        result = module.execute(NEW, resume=False)
        once(ROOT / 'results' / (LABEL + '.json'), result)
        save(handle_path, {**prior, 'pid': os.getpid(), 'status': 'completed', 'finished_epoch': time.time()})
    except Exception as exc:
        save(handle_path, {**prior, 'pid': os.getpid(), 'status': 'failed', 'diagnostic': type(exc).__name__ + ': ' + str(exc)[:2000]})
        raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(); mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare-only', action='store_true')
    mode.add_argument('--launch-once', action='store_true')
    mode.add_argument('--execute-prepared', action='store_true')
    args = parser.parse_args()
    prepare() if args.prepare_only else launch_once() if args.launch_once else execute()


if __name__ == '__main__':
    main()
