"""No paid provider traffic; exercise actual workers and production controller."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / 'tools/validation/coral_directed.py'
spec = importlib.util.spec_from_file_location('coral_directed_qualification', SCRIPT)
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


@pytest.fixture
def broker(tmp_path, monkeypatch):
    item = qualification.Broker(tmp_path / 'broker', mode='fixture', model='fixture')
    item.b_waiting.set()  # unit-only admission setup; integration uses actual B registration
    item.started = {('a', 'unit-a'): 10001, ('b', 'unit-b'): 10002}
    monkeypatch.setattr(item, 'host_process', lambda pid: {'pid': pid, 'synthetic_unit_test_only': True})
    item.thread.start()
    try:
        yield item
    finally:
        item.close()


def envelope(slot='a:0', *, mode='fixture', **changes):
    body = {'model': 'fixture', 'temperature': 0, 'max_tokens': 4096,
        'thinking': {'type': 'disabled'}, 'messages': [{'role': 'user', 'content': 'test\n' + json.dumps({
            'transport_mode': mode, 'qualification_plan_sha256': 'unit', 'worker': slot.split(':')[0]})}]}
    body.update(changes)
    return {'slot': slot, 'body': body}


def test_completed_slot_is_cached_without_a_second_dispatch(broker):
    first = broker.dispatch(envelope())
    assert broker.dispatch(envelope()) == first
    assert len(broker.rows()) == 1
    with pytest.raises(ValueError, match='changed'):
        broker.dispatch(envelope(messages=[{'role': 'user', 'content': 'test\n' + json.dumps({
            'transport_mode': 'fixture', 'qualification_plan_sha256': 'unit', 'worker': 'a', 'extra': 'tampered'})}]))
    assert len(broker.rows()) == 1


@pytest.mark.parametrize('changes', [{'max_tokens': 4097}, {'max_tokens': 4096.0},
    {'model': 'other-model'}, {'thinking': {'type': 'enabled'}}, {'temperature': 1}])
def test_request_limits_are_checked_before_reserving_a_slot(broker, changes):
    with pytest.raises(ValueError, match='frozen protocol'):
        broker.dispatch(envelope(**changes))
    assert not broker.rows()


def test_exact_three_slots_and_no_unapproved_fourth(broker):
    for slot in qualification.SLOTS:
        broker.dispatch(envelope(slot))
    with pytest.raises(ValueError, match='undeclared'):
        broker.dispatch(envelope('b:1'))
    assert len(broker.rows()) == 3


def test_unknown_attempt_is_never_forwarded_again(broker, monkeypatch):
    broker.mode = 'live'  # unit-only injected timeout; this incomplete run cannot pass audit
    attempts = []

    def timeout(raw):
        attempts.append(raw)
        raise TimeoutError('synthetic fixture timeout; no network')

    monkeypatch.setattr(broker, 'forward', timeout)
    with pytest.raises(TimeoutError):
        broker.dispatch(envelope(mode='live'))
    assert broker.rows()[0]['status'] == 'unknown'
    with pytest.raises(ValueError, match='uncertain'):
        broker.dispatch(envelope(mode='live'))
    assert len(attempts) == 1 and len(broker.rows()) == 1


def test_negative_raw_prompt_usage_is_retained_unknown_not_zero(broker, monkeypatch):
    broker.mode = 'live'
    broker.api_key = 'synthetic-test-value-not-a-provider-credential'
    answer = {'id': 'synthetic-reply', 'model': 'fixture', 'choices': [],
              'usage': {'prompt_tokens': -1, 'completion_tokens': 1}}
    monkeypatch.setattr(broker, 'forward', lambda raw: (qualification.canonical(answer),
        {'network_forwarded': True, 'transport': 'https', 'status': 200,
         'url': 'https://api.deepseek.com/chat/completions'}))
    with pytest.raises(ValueError, match='usage differs'):
        broker.dispatch(envelope(mode='live'))
    assert broker.rows()[0]['status'] == 'unknown'
    assert qualification.load(broker.root / 'a-0-failed-attempt.json')['unknown_is_NOT_zero']
    assert (broker.root / 'a-0-http-response.bin').is_file()


def test_live_run_without_reviewed_approval_fails_before_output_or_key_access(tmp_path, monkeypatch):
    # The first preflight checks HEAD:proteus, then an empty diff.
    monkeypatch.setattr(qualification, 'git', lambda *args:
        '' if args[0] == 'diff' else qualification.PRODUCTION_TREE)
    target = tmp_path / 'not-created'
    args = SimpleNamespace(out=target, model='fixture', mode='live',
        execute_approved_live=False, approval=None)
    with pytest.raises(ValueError, match='explicit reviewed approval'):
        qualification.run(args)
    assert not target.exists()


@pytest.fixture(scope='module')
def completed(tmp_path_factory):
    args = SimpleNamespace(out=tmp_path_factory.mktemp('coral-directed') / 'qualification', model='fixture', mode='fixture',
        execute_approved_live=False, approval=None)
    qualification.run(args)
    return args.out


def test_actual_workers_shared_request_intervention_and_controller_hard_exit(completed):
    accepted = qualification.load(completed / 'acceptance.json')
    assert accepted['real_paid_provider_dispatches'] == 0
    assert accepted['model_transport_dispatches'] == 3
    assert accepted['cross_worker_publication_in_actual_B_request']
    assert accepted['one_intervention_in_actual_A_next_generation_request']
    assert accepted['controller_actual_hard_exit_code'] == 86
    assert accepted['same_workspace_resume_count'] == 1
    assert accepted['no_duplicate_native_settlement']
    assert accepted['native_command_worker_model_budget_NOT_falsely_claimed_bounded']
    assert accepted['physical_attempt_cap_enforced_by_durable_broker'] == 3
    assert accepted['native_cost_unknowns_unchanged']['charged']['cost_usd'] is None
    manifest = qualification.load(completed / 'recipe.json')
    assert manifest['execution']['max_workers'] == 1
    assert all(not w['runtime']['options']['credential_env'] for w in manifest['workers'])
    before = qualification.load(completed / 'controller-hard-exit.json')['broker_rows_before']
    with qualification.sqlite3.connect(completed / 'broker/dispatches.sqlite3') as db:
        db.row_factory = qualification.sqlite3.Row
        after = [dict(row) for row in db.execute('SELECT * FROM dispatches ORDER BY slot')]
    assert before == after
    assert set(accepted['request_bindings'][0]) >= {'launch_hash', 'body_sha256', 'provider_id'}


def broker_view(root):
    def rows():
        with qualification.sqlite3.connect(root / 'broker/dispatches.sqlite3') as db:
            db.row_factory = qualification.sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM dispatches ORDER BY slot')]
    return SimpleNamespace(root=root / 'broker', mode=qualification.load(root / 'exclusive-start-claim.json')['mode'], rows=rows)


def test_auditor_rejects_tampered_actual_shared_request(completed):
    workspace = next(p.parent for p in (completed / 'run/workers').glob('*/workspace/model-0-intent.json')
        if qualification.load(p)['slot'] == 'b:0')
    path = workspace / 'model-0-request.json'
    original = path.read_bytes()
    try:
        body = json.loads(original)
        body['messages'][0]['content'] = body['messages'][0]['content'].replace(qualification.SHARED_MARKER, 'omitted')
        path.write_bytes(qualification.canonical(body))
        with pytest.raises(AssertionError):
            qualification.audit(completed, broker_view(completed))
    finally:
        path.write_bytes(original)


def test_auditor_rejects_tampered_publication_author(completed):
    workspace = next(p.parent for p in (completed / 'run/workers').glob('*/workspace/model-0-intent.json')
        if qualification.load(p)['slot'] == 'b:0')
    path = next(p for p in workspace.glob('knowledge-*.json')
        if qualification.load(p)['revision']['write']['key'] == 'a-strategy')
    original = path.read_bytes()
    try:
        record = json.loads(original)
        record['revision']['write']['writer_id'] = 'b'
        path.write_bytes(qualification.canonical(record))
        with pytest.raises(AssertionError):
            qualification.audit(completed, broker_view(completed))
    finally:
        path.write_bytes(original)


def test_duplicate_or_missing_settlement_fails():
    entries = [{'id': 'effect', 'settlement': {'actual': 'known'}}]
    one = {'kind': 'settled', 'entry': 'effect'}
    assert qualification.verify_settlements([one], entries) == 1
    with pytest.raises(AssertionError):
        qualification.verify_settlements([one, one], entries)
    with pytest.raises(AssertionError):
        qualification.verify_settlements([], entries)


def test_invalid_approval_cannot_create_a_second_live_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(qualification, 'git', lambda *args:
        '' if args[0] == 'diff' else qualification.PRODUCTION_TREE)
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps({'approved': True, 'plan_sha256': 'wrong',
        'max_physical_dispatches': 3, 'output_root': str(tmp_path / 'not-created')}))
    args = SimpleNamespace(out=tmp_path / 'not-created', model='fixture', mode='live',
        execute_approved_live=True, approval=approval)
    with pytest.raises(ValueError, match='exact plan'):
        qualification.run(args)
    assert not args.out.exists() and not approval.with_name(approval.name + '.used.json').exists()


@pytest.mark.parametrize('mutation', ['mode_flip', 'model_drift', 'missing_plan_hash', 'changed_plan_hash'])
def test_auditor_rejects_fixture_relabel_and_plan_drift(completed, mutation):
    path = completed / ('plan.json' if mutation == 'model_drift' else 'exclusive-start-claim.json')
    original = path.read_bytes()
    try:
        value = json.loads(original)
        if mutation == 'mode_flip':
            value['mode'] = 'live'
        elif mutation == 'model_drift':
            value['model'] = 'another-model'
        elif mutation == 'missing_plan_hash':
            del value['plan_sha256']
        else:
            value['plan_sha256'] = '0' * 64
        path.write_bytes(qualification.canonical(value))
        with pytest.raises((AssertionError, KeyError)):
            qualification.audit(completed, broker_view(completed))
    finally:
        path.write_bytes(original)


def test_auditor_rejects_changed_raw_response_usage(completed):
    path = completed / 'broker/a-0-http-response.bin'
    original = path.read_bytes()
    try:
        value = json.loads(original)
        value['usage']['prompt_tokens'] = -1
        path.write_bytes(qualification.canonical(value))
        with pytest.raises(AssertionError):
            qualification.audit(completed, broker_view(completed))
    finally:
        path.write_bytes(original)


def test_audit_copy_removes_credentials_without_changing_native_proof(completed, tmp_path):
    destination = tmp_path / 'audit-copy'
    index = qualification.export_audit_copy(completed, destination)
    assert not index['failures']
    assert not (destination / 'broker/permit.txt').exists()
    queues = list(destination.glob('run/private/gateways/*/queue.sqlite3'))
    assert len(queues) == 1
    with qualification.sqlite3.connect(queues[0]) as db:
        assert db.execute('SELECT COUNT(*) FROM credentials').fetchone()[0] == 0
    result = qualification.audit(destination, broker_view(destination))
    assert result['real_paid_provider_dispatches'] == 0
    assert result['cross_worker_publication_in_actual_B_request']


def test_export_refuses_credential_shaped_payload_and_retains_failure_receipt(tmp_path):
    source, destination = tmp_path / 'source', tmp_path / 'copy'
    source.mkdir()
    (source / 'failure.json').write_text('{"status":"unknown","paid_calls":null}')
    (source / 'unsafe.txt').write_text('sk-' + 'x' * 32)
    index = qualification.export_audit_copy(source, destination)
    assert index['incomplete_or_private_payload_refused']
    assert not (destination / 'unsafe.txt').exists()
    assert qualification.load(destination / 'failure.json')['paid_calls'] is None
