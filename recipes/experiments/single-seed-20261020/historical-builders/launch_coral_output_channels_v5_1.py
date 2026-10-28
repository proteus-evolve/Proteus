"""One root-selected, fresh original-H0 CORAL arm; never an automatic queue.

This launcher is external to the frozen science recipe. Each exact label needs
its own independent root marker AND current route admission. Invocation is
exclusive, never retries or resumes, and cannot create either authorization.
The two commands must be run sequentially; arm two requires arm one's complete
20-submission execution/report. This file never calibrates or executes old
model candidates, strips stdout noise, changes model prompts, or regrades old
paid runs. Old nulls, unknown charges and held processes remain untouched.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'sources/coral-output-channels-v5_1'
sys.path.insert(0, str(SOURCE))
from proteus.benchmarks.coral_circle_packing.recipe import register
from proteus.benchmarks.coral_circle_packing.report import report_packing
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import TaskDataset
from proteus.methods.coral.report import report_coral
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.cli import source_pin
from proteus.recipes.config import load_recipe
from proteus.recipes.export import report_run
from proteus.recipes.manifest import DependencyLock
from proteus.recipes.runner import run_recipe

SOURCE_SHA = '7ca7742d18f267b5a2805dad59979e328d3a8f36'
PREP_SHA = '3ad8c6c44a1f16e25ce0bdff9513da407706e14b5638a7ad55f28dc8e40f5427'
AUDIT_SHA = '7c210d8f149e6267a9f10c9a5a87c92668199c14afb79e8b8e54cf5c02b3b6f3'
GATE_SHA = '77c9c8125b4441f3f7317213bf64d6472522952e4c34fef0d1efee5e2ba66065'
QUALIFIER_SHA = '4c5d51e0c3a69cb79847e8031f505b12d90ddd275f875797d31f28d3182ff2ca'
PROBE_SHA = '64500917b70b3704534dbab7b4c83b2e20a27f9d4ca83c85f59f21f1098dd467'
SDK_SHA = '77911f4df7fe29850d006ec969786a920302668fa7cdf35ff7c31cc96337bf65'
WORKER_SHA = '2ab9ba3e043c19967b2218bb614b64ae0bc9c81ce892c77c6517005dd309e031'
FEEDBACK_SHA = '2df07d4d6ab5169f16f7e51385ba29278ba9edfff149e102d9b9e8590c6e3250'
H0 = 'sha256:0991f8ff72215ee85f77c94e331066b1647ff2d264a97a6a7730f2c6f6241a40'
LABELS = tuple(f'linux-coral-circle-{workers}-workers-v5-1' for workers in (1, 2))
SDK = ROOT / 'harbor-venv/lib/python3.12/site-packages/harbor/environments/docker/docker.py'
PINNED_FILES = {
    'preparation_helper_sha256': (ROOT / 'prepare_coral_output_channels_v5_1.py', PREP_SHA),
    'prepared_profiles_audit_sha256': (ROOT / 'coral-v5-1-prepared-profiles-audit.json', AUDIT_SHA),
    'runtime_gate_sha256': (ROOT / 'qualified-coral-output-channels-v5-1.json', GATE_SHA),
    'runtime_gate_helper_sha256': (ROOT / 'qualify_coral_output_channels_v5_1.py', QUALIFIER_SHA),
    'targeted_runtime_probe_sha256': (ROOT / 'qualified-coral-output-channel-probe-v5-1.json', PROBE_SHA),
    'installed_Harbor_SDK_sha256': (SDK, SDK_SHA),
    'worker_sha256': (ROOT / 'circle_linux_worker_v4_1.py', WORKER_SHA),
    'feedback_helper_sha256': (ROOT / 'circle_released_feedback_v4_1.py', FEEDBACK_SHA),
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as output:
        output.write(json.dumps(value, allow_nan=False, indent=2) + '\n')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.pending')
    with temporary.open('x') as output:
        output.write(json.dumps(value, allow_nan=False, indent=2) + '\n')
    temporary.replace(path)


def verify_bundle(label):
    assert label in LABELS
    source = source_pin('proteus', SOURCE)
    assert source.revision == SOURCE_SHA and source.dirty_patch_hash is None
    assert Path(register.__code__.co_filename).resolve().is_relative_to(SOURCE.resolve())
    hashes = {}
    for key, (path, expected) in PINNED_FILES.items():
        assert sha(path) == expected, key + ' changed'
        hashes[key] = expected
    audit = json.loads(PINNED_FILES['prepared_profiles_audit_sha256'][0].read_text())
    assert audit['status'] == 'two_full_original_H0_profiles_prepared_not_paid_released'
    assert audit['source_sha'] == SOURCE_SHA and audit['paid_provider_calls'] == 0
    profiles = {item['label']: item for item in audit['profiles']}
    assert set(profiles) == set(LABELS)
    profile = profiles[label]
    assert profile['normalized_scientific_comparison']['normalized_scientific_differences'] == []
    assert profile['gate_sha256'] == GATE_SHA
    assert profile['worker_sha256'] == WORKER_SHA and profile['feedback_helper_sha256'] == FEEDBACK_SHA
    for item in profile['files']:
        assert sha(Path(item['path'])) == item['sha256']
    gate = json.loads(PINNED_FILES['runtime_gate_sha256'][0].read_text())
    assert gate['status'] == 'passed_actual_unpaid_Harbor_channel_separation_and_unavailable_score_gates'
    assert gate['source_sha'] == SOURCE_SHA and gate['paid_provider_requests'] == 0
    assert gate['actual_subject_trials'] == 9 and gate['helper_sha256'] == QUALIFIER_SHA
    assert gate['installed_Harbor_SDK_sha256_before_after_unchanged'] == SDK_SHA
    root = Path(profile['root'])
    recipe = load_recipe(root / 'recipe.json')
    lock = DependencyLock.from_json((root / 'lock.json').read_text())
    registry = builtin_registry(); register(registry); registry.check_lock(recipe, lock)
    assert recipe.config_hash == profile['config_hash'] and content_hash(lock) == profile['lock_hash']
    assert recipe.subject.initial_artifact.content_hash == H0
    assert recipe.subject.initial_artifact.to_dict() == profile['initial_H0']
    assert recipe.seed == 0 and recipe.method.options['max_submissions'] == 20
    assert recipe.execution.timeout_seconds == 600
    assert dict(recipe.execution.environment.resources) == {'cpus': 1, 'memory_mb': 2048}
    assert recipe.execution.environment.backend.options['source_build']['timeout_seconds'] == 900
    workers = profile['workers']
    assert workers in (1, 2) and len(recipe.workers) == workers
    for worker in recipe.workers:
        command = worker.runtime.options['command']
        assert command[1] == str(ROOT / 'circle_linux_worker_v4_1.py')
        assert command[2] == str(20 // workers) and command[4] == label
        for path, expected in worker.runtime.options['command_files'].items():
            assert 'sha256:' + sha(Path(path)) == expected
    for name, expected in profile['same_original_H0_file_bytes'].items():
        assert sha(root / 'artifact-store/blobs' / expected) == expected
    for name, expected in profile['same_original_task_bytes'].items():
        assert sha(root / 'task' / name) == expected
    dataset = TaskDataset.from_json((root / 'tasks.json').read_text())
    assert all(item.tasks.dataset_hash == content_hash(dataset) for item in recipe.evaluations)
    return profile, hashes, recipe, lock, registry


def accepted(label):
    profile, hashes, recipe, lock, registry = verify_bundle(label)
    marker_path = ROOT / 'root-acceptances' / (label + '-release-accepted.json')
    marker = json.loads(marker_path.read_text())
    assert marker.get('accepted') is True and marker.get('label') == label
    assert marker.get('scope') == 'single_prepared_profile_fresh_start_only'
    assert marker.get('launcher_sha256') == sha(Path(__file__))
    assert marker.get('source_sha') == SOURCE_SHA
    assert marker.get('config_hash') == profile['config_hash'] and marker.get('lock_hash') == profile['lock_hash']
    assert all(marker.get(key) == digest for key, digest in hashes.items())
    assert marker.get('automatic_other_profile_dispatch') is False
    assert marker.get('automatic_paid_retry_or_resume') is False
    return profile, hashes, recipe, lock, registry, marker_path


def require_fresh(profile):
    label = profile['label']
    handle = json.loads((ROOT / 'handles' / (label + '.json')).read_text())
    assert handle['status'] == 'prepared_not_dispatched'
    assert not (Path(profile['root']) / 'run').exists(), 'No automatic repeat or resume'
    if label == LABELS[1]:
        first = json.loads((ROOT / 'results' / (LABELS[0] + '.json')).read_text())
        first_handle = json.loads((ROOT / 'handles' / (LABELS[0] + '.json')).read_text())
        assert first_handle['status'] == 'completed'
        assert first['status'] == 'completed_full_protocol_pending_independent_acceptance'
        assert first['cooperation']['stop_reason'] == 'submission_limit'
        assert len(first['cooperation']['submissions']) == 20
    return handle


def launch(label):
    profile, hashes, _, _, _, marker = accepted(label)
    require_fresh(profile)
    intent = ROOT / (label + '-launch-intent.json')
    receipt = ROOT / (label + '-launch.json')
    claim = ROOT / (label + '-execute-claim.json')
    assert not intent.exists() and not receipt.exists() and not claim.exists()
    token = (ROOT / 'private-bridge/scoped-token.txt').read_text().strip()
    request = urllib.request.Request('http://127.0.0.1:62125/admission/' + label,
        headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert json.load(response).get('allowed') is True, 'Root route release still required'
    once(intent, {'label': label, 'created_epoch': time.time(), 'launcher_sha256': sha(Path(__file__)),
        'marker_sha256': sha(marker), 'config_hash': profile['config_hash'],
        'lock_hash': profile['lock_hash'], 'retry': False, 'resume': False})
    log = ROOT / 'logs' / (label + '.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('xb') as output:
        process = subprocess.Popen([sys.executable, '-u', str(Path(__file__)), '--execute-prepared',
            '--label', label], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True)
    record = {'label': label, 'status': 'launched_not_full_result', 'pid': process.pid,
        'source_sha': SOURCE_SHA, 'started_epoch': time.time(), 'config_hash': profile['config_hash'],
        'lock_hash': profile['lock_hash'], 'launcher_sha256': sha(Path(__file__)),
        'root_marker_sha256': sha(marker), **hashes, 'resume': False, 'retry': False,
        'initialization': 'original H0 only; no old replies/candidates/private scores',
        'total_model_proposals': 20, 'sequential_arm_order': 'one worker20, then two workers10each',
        'other_profiles_automatically_launched': 0, 'no_new_real_model_calibration': True,
        'log': str(log), 'independent_full_result_acceptance': 'still required'}
    once(receipt, record)
    print(json.dumps(record), flush=True)


def execute(label):
    profile, _, recipe, lock, registry, marker = accepted(label)
    intent = json.loads((ROOT / (label + '-launch-intent.json')).read_text())
    assert intent['label'] == label and intent['launcher_sha256'] == sha(Path(__file__))
    assert intent['marker_sha256'] == sha(marker)
    once(ROOT / (label + '-execute-claim.json'), {'label': label, 'pid': os.getpid(), 'epoch': time.time()})
    prior = require_fresh(profile)
    root = Path(profile['root'])
    handle = ROOT / 'handles' / (label + '.json')
    save(handle, {**prior, 'pid': os.getpid(), 'status': 'running', 'started_epoch': time.time(),
        'resume': False, 'original_H0': H0})
    try:
        result = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run',
            artifact_source=root / 'artifact-store', resume=False)
        once(ROOT / 'results' / (label + '-execution.json'), result.to_dict())
        assert result.status.value == 'completed', result.diagnostic
        common = report_run(root / 'run', include_private=True)
        cooperation = report_coral(root / 'run', include_private=True)
        once(ROOT / 'results' / (label + '-formal-outcomes.json'), common)
        once(ROOT / 'results' / (label + '-cooperation.json'), cooperation)
        assert cooperation['stop_reason'] == 'submission_limit' and len(cooperation['submissions']) == 20
        try:
            geometry = report_packing(root / 'run', include_private=True)
            geometry_status = 'domain_geometry_regraded'
        except Exception as exc:
            geometry = None
            geometry_status = 'Unavailable domain renderer: ' + type(exc).__name__ + ': ' + str(exc)[:1000]
        once(ROOT / 'results' / (label + '.json'), {
            'status': 'completed_full_protocol_pending_independent_acceptance',
            'formal': common, 'cooperation': cooperation, 'geometry': geometry,
            'domain_report_status': geometry_status, 'unavailable_formal_scores_are_not_zero': True,
            'same_task_confirmation_is_not_generalization': True,
            'original_model_controller_prompt_H0_and_limits_preserved': True})
        save(handle, {**prior, 'pid': os.getpid(), 'status': 'completed', 'finished_epoch': time.time(),
            'independent_full_result_acceptance': 'still required'})
    except BaseException as exc:
        save(handle, {**prior, 'pid': os.getpid(), 'status': 'failed',
            'diagnostic': type(exc).__name__ + ': ' + str(exc)[:2000],
            'automatic_retry_resume_or_next_arm': False})
        raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--label', required=True, choices=LABELS)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--inspect-prepared', action='store_true')
    mode.add_argument('--launch-once', action='store_true')
    mode.add_argument('--execute-prepared', action='store_true')
    args = parser.parse_args()
    if args.inspect_prepared:
        profile, hashes, _, _, _ = verify_bundle(args.label)
        print(json.dumps({'status': 'static_full_bundle_verified_not_root_released_or_dispatched',
            'label': args.label, 'config_hash': profile['config_hash'], 'lock_hash': profile['lock_hash'],
            'launcher_sha256': sha(Path(__file__)), **hashes,
            'credentials_original_ledger_and_model_calls_accessed': 0}), flush=True)
    elif args.launch_once:
        launch(args.label)
    else:
        execute(args.label)


if __name__ == '__main__':
    main()
