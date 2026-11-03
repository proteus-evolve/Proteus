"""Fresh-H0 CORAL profiles with explicit executable-Python output contract.

Only the frozen core stays unchanged. The new content-pinned worker/helper have
the old logic with a prompt-only instructions correction and versioned import.
Qualification lineage is disclosed, not relabeled as40new controller trials.
Calibration output, old candidates and old feedback never initialize this run.
"""
from dataclasses import replace
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'sources/coral'
sys.path.insert(0, str(SOURCE))
from proteus.benchmarks.coral_circle_packing.recipe import prepare, register
from proteus.benchmarks.coral_circle_packing.report import report_packing
from proteus.core.serialization import content_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.cli import source_pin, installed_packages
from proteus.recipes.config import load_recipe
from proteus.recipes.manifest import SourcePin, DependencyLock
from proteus.recipes.export import report_run
from proteus.methods.coral.report import report_coral
from proteus.recipes.runner import run_recipe

EXPECTED_SOURCE = '722ac71a36ab355f6512afb3214f05722eb90b9b'
EXPECTED_WORKER = '2ab9ba3e043c19967b2218bb614b64ae0bc9c81ce892c77c6517005dd309e031'
EXPECTED_FEEDBACK = '2df07d4d6ab5169f16f7e51385ba29278ba9edfff149e102d9b9e8590c6e3250'
EXPECTED_H0 = 'sha256:0991f8ff72215ee85f77c94e331066b1647ff2d264a97a6a7730f2c6f6241a40'
ORIENTATION = (
    'You are continuing the same CORAL research worker in a new model context, '
    'not resuming a native conversation session. Your workspace, candidate/, '
    'immutable model responses, submission receipts, formal feedback, and shared '
    'knowledge are retained. Inspect them and reuse already sealed responses '
    'without another model request. Continue from the first unfinished proposal '
    'under the existing total proposal limit. Apply the controller intervention '
    'below; do not invent evaluations, discard history, or reset the budget.'
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.pending')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def evidence_chain():
    names = ('qualified-linux-coral.json', 'qualified-linux-coral-full-semantic-v3_1.json',
        'qualified-coral-peer-and-pause-v3_1.json', 'qualified-coral-delayed-component-v3_1.json',
        'qualified-coral-prompt-integration-v4_1.json', 'qualified-coral-real-output-contract-v4_1.json')
    records = {name: json.loads((ROOT / name).read_text()) for name in names}
    grade, lineage, peer, component, integration, calibration = (records[name] for name in names)
    assert all(value['status'] == 'passed' for value in (grade, lineage, peer, component, integration))
    assert lineage['source_sha'] == EXPECTED_SOURCE and lineage['real_provider_calls'] == 0
    assert len(lineage['arms']) == 2 and all(arm['formal_submissions'] == 20 and arm['semantic_next_HTTP_requests_verified'] for arm in lineage['arms'])
    assert integration['real_provider_calls'] == 0 and integration['actual_worker_fake_HTTP_requests'] == 2
    assert integration['helper_AST_identical_except_render_prompt_instructions_assignment'] is True
    assert integration['worker_AST_identical_except_versioned_import_path_and_docstring'] is True
    assert integration['feedback_parsing_budget_persistence_submission_pause_resume_logic_unchanged'] is True
    assert calibration['status'] == 'passed_AST_output_contract_only' and calibration['real_provider_calls'] == 1
    assert calibration['host_candidate_execution'] is False and calibration['search_warm_start'] is False
    for item in (integration, calibration):
        assert item['worker_sha256'] == EXPECTED_WORKER and item['helper_sha256'] == EXPECTED_FEEDBACK
    assert sha(ROOT / 'circle_linux_worker_v4_1.py') == EXPECTED_WORKER
    assert sha(ROOT / 'circle_released_feedback_v4_1.py') == EXPECTED_FEEDBACK
    return {'files': {name: {'path': str(ROOT / name), 'sha256': sha(ROOT / name), 'status': records[name]['status']} for name in names},
        'scope': 'Old v3_1 actual20/arm full-controller+cross-peer+separately scoped delayed-component; NEW v4_1 AST differential+actual2HTTP-request component+ONE unscored real-model JSON/AST calibration. NOT40new controller trials or geometry qualification of calibration output.',
        'strict_controller_inflight_scope': 'Witness only two-worker a; separate delayed Runtime component covers generic pause/seal/fresh-resume, not all controller races.'}


def prepare_one(count, chain):
    assert count in (1, 2)
    label = f'linux-coral-circle-{count}-workers-v4-1'
    root = ROOT / 'formal' / label
    assert not root.exists(), 'Never replace old or prepared scientific evidence'
    worker = ROOT / 'circle_linux_worker_v4_1.py'
    feedback = ROOT / 'circle_released_feedback_v4_1.py'
    executable = str(Path(sys.executable).resolve())
    runtime = plugin('proteus.worker.command',
        command=[executable, str(worker), str(20 // count), 'http://127.0.0.1:62125', label, '700', 'sha256:' + EXPECTED_FEEDBACK],
        command_files={str(path): 'sha256:' + sha(Path(path)) for path in (executable, worker, feedback)},
        timeout_seconds=43200, shutdown_grace_seconds=700)
    path = prepare(root, str(ROOT / 'harbor-venv/bin/python'), worker_runtime=runtime,
        workers=count, submissions=20, seed=0)
    recipe = load_recipe(path)
    assert recipe.subject.initial_artifact.content_hash == EXPECTED_H0
    recipe = replace(recipe,
        method=plugin('coral.single_island', **{**recipe.method.options, 'stop_timeout_seconds': 900}),
        event_policies=tuple(replace(policy, pause_timeout_seconds=720, resume_policy='fresh', fresh_orientation=ORIENTATION)
            for policy in recipe.event_policies),
        reference=replace(recipe.reference, deviations=tuple(recipe.reference.deviations) + (
            'Separate Linux v4_1 output-contract-corrected scientific profiles, originalH0 only, seed0: exactly20proposals total per arm,1worker20vs2workers10each; original5/10/5cadence/private geometry evaluator. No old candidates/replies/feedback or calibration output warm-start.',
            'Frozen core722ac71a unchanged. NEW pinned worker/helper: AST differential proves only prompt instructions and versioned import origin/docstring changed from v3_1; feedback parsing, runtime controls, submissions, budgets and idempotency unchanged.',
            'Prompt requests full actual executable Python source and a run() returning26centers/radii/sum; construct_packing optional, not an added external API. Real-model calibration is ONE separately labelled unscored JSON/AST-only call, with no host execution or geometry verification, not search progress.',
            'Scientific worker performs no AST-based filtering, repair, resampling, H0 substitution or automatic paid retry. Actual invalid replies and null outcomes remain raw; malformed JSON may fail a worker/condition rather than fabricate a score.',
            'Command worker uses explicit persistent-workspace fresh fallback plus orientation, not native session IDs. Same stable requestIDs and600s HTTP/700s shutdown/720s pause/900s stop; sealed responses never automatically resent.',
            'Same Linux Docker1CPU/2048MiB,600s candidate/900s build/30s private verifier, public task network, pinned NumPy/SciPy shared dependencies; no geometry/scoring/budget change.',
            'Released authorized evidence only, mandatory latest-first measured nullable score/outcome/runtime/diagnostics; outer completion is not fitness success, model notes are unverified. Same8shared slots, most-recent first observation priority, no claimed sender chronology, exact prompt/reply/source refs retained.',
            'Qualification lineage: prior v3_1 actual20/arm full-controller/shared-feedback/completed-resume plus separately scoped delayed-response component; new v4_1 AST differential and actual2next HTTP-request component plus ONE real AST calibration. No claim that40new controller trials ran, or all controller pause races were observed.',
            'Preparation/qualification/startup is not optimization benefit. Final confirmation repeats the same task with a fresh trial seed; it is NOT generalization.',
        )))
    registry = builtin_registry(); register(registry)
    recipe = registry.resolve(recipe)
    source = source_pin('proteus', SOURCE)
    assert source.revision == EXPECTED_SOURCE and source.dirty_patch_hash is None
    helpers = {path.name: sha(path) for path in (Path(__file__), worker, feedback)}
    helper_pin = SourcePin(name='linux-coral-runtime-v4-1',
        repository='local-content-addressed:LinuxCORALexplicitPythonOutputContract',
        revision=content_hash(helpers).split(':')[1])
    lock = registry.make_lock(recipe, [source, helper_pin], packages=installed_packages())
    path.write_text(recipe.to_json() + '\n')
    (root / 'lock.json').write_text(lock.to_json() + '\n')
    save(root / 'full-protocol.json', {'label': label, 'source_sha': source.revision,
        'config_hash': recipe.config_hash, 'lock_hash': content_hash(lock), 'seed': 0, 'replicates': 1,
        'workers': count, 'proposals_per_worker': 20 // count, 'model_proposals_total': 20,
        'formal_submissions_total': 20, 'candidate_seconds': 600, 'build_seconds': 900,
        'private_verifier_seconds': 30, 'CPU': 1, 'memory_mib': 2048, 'network': 'public',
        'resume_policy': 'fresh', 'fresh_orientation': ORIENTATION, 'native_session_claimed': False,
        'initialization': 'OriginalH0 only; no old search/calibration candidates, replies or feedback',
        'initial_artifact_hash': EXPECTED_H0,
        'model': {'alias': 'deepseek-flash', 'temperature': 0, 'max_tokens': 8192, 'thinking': 'disabled'},
        'feedback_transport_version': 3, 'output_contract_version': '4_1', 'helper_hashes': helpers,
        'qualification_evidence_chain': chain,
        'no_generalization_claim': True, 'fixture': False, 'calibration_warm_start': False,
        'scientific_invalid_output_filter_resample_or_repair': False})
    save(ROOT / 'handles' / (label + '.json'), {'label': label, 'pid': os.getpid(),
        'status': 'prepared_not_dispatched', 'root': str(root), 'source_sha': source.revision, 'workers': count})
    return label, root, recipe, lock, registry


def load_prepared(count, chain):
    label = f'linux-coral-circle-{count}-workers-v4-1'
    root = ROOT / 'formal' / label
    handle = json.loads((ROOT / 'handles' / (label + '.json')).read_text())
    assert handle['status'] == 'prepared_not_dispatched', 'No automatic repeat/resume'
    recipe = load_recipe(root / 'recipe.json')
    lock = DependencyLock.from_json((root / 'lock.json').read_text())
    registry = builtin_registry(); register(registry); registry.check_lock(recipe, lock)
    protocol = json.loads((root / 'full-protocol.json').read_text())
    assert protocol['qualification_evidence_chain'] == chain
    assert protocol['config_hash'] == recipe.config_hash and protocol['lock_hash'] == content_hash(lock)
    assert protocol['helper_hashes'] == {name: sha(ROOT / name) for name in ('run_coral_formal_v4_1.py', 'circle_linux_worker_v4_1.py', 'circle_released_feedback_v4_1.py')}
    assert recipe.subject.initial_artifact.content_hash == EXPECTED_H0
    source = source_pin('proteus', SOURCE)
    assert source.revision == EXPECTED_SOURCE and source.dirty_patch_hash is None
    return label, root, recipe, lock, registry


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare-only', action='store_true')
    mode.add_argument('--execute-prepared', action='store_true')
    args = parser.parse_args()
    chain = evidence_chain()
    prepared = [prepare_one(count, chain) if args.prepare_only else load_prepared(count, chain) for count in (1, 2)]
    if args.prepare_only:
        print(json.dumps({'status': 'prepared_not_dispatched', 'labels': [value[0] for value in prepared],
            'runner_sha256': sha(Path(__file__)), 'evidence_chain': chain}), flush=True)
        return
    accepted = json.loads((ROOT / 'CORAL-v4_1-scientific-release-accepted.json').read_text())
    assert accepted.get('accepted') is True and accepted.get('runner_sha256') == sha(Path(__file__))
    assert accepted.get('worker_sha256') == EXPECTED_WORKER and accepted.get('helper_sha256') == EXPECTED_FEEDBACK
    for label, root, recipe, lock, registry in prepared:
        save(ROOT / 'handles' / (label + '.json'), {'label': label, 'pid': os.getpid(), 'status': 'running',
            'root': str(root), 'source_sha': EXPECTED_SOURCE, 'started_epoch': time.time(),
            'resume_policy': 'fresh', 'feedback_transport_version': 3, 'output_contract_version': '4_1'})
        result = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run', artifact_source=root / 'artifact-store')
        save(ROOT / 'results' / (label + '-execution.json'), result.to_dict())
        save(ROOT / 'handles' / (label + '.json'), {'label': label, 'pid': os.getpid(), 'status': result.status.value,
            'root': str(root), 'source_sha': EXPECTED_SOURCE, 'diagnostic': result.diagnostic})
        assert result.status.value == 'completed', result.diagnostic
        common = report_run(root / 'run', include_private=True)
        cooperation = report_coral(root / 'run', include_private=True)
        save(ROOT / 'results' / (label + '-formal-outcomes.json'), common)
        save(ROOT / 'results' / (label + '-cooperation.json'), cooperation)
        assert cooperation['stop_reason'] == 'submission_limit' and len(cooperation['submissions']) == 20
        try:
            geometry = report_packing(root / 'run', include_private=True)
            domain_status = 'domain_geometry_regraded'
        except Exception as exc:
            geometry = None
            domain_status = 'Unavailable domain renderer: ' + type(exc).__name__ + ': ' + str(exc)[:1000]
        save(ROOT / 'results' / (label + '.json'), {'status': 'completed_full_protocol_pending_independent_acceptance',
            'formal': common, 'cooperation': cooperation, 'geometry': geometry, 'domain_report_status': domain_status,
            'unavailable_formal_scores_are_not_zero': True, 'same_task_confirmation_is_not_generalization': True})


if __name__ == '__main__':
    main()
