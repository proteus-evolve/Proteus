"""Bounded, current-source CORAL qualification; fixture by default, never a benchmark.

Three model slots: A initial proposal, A post-intervention reflection, B shared
proposal. Two actual command-worker processes, one evaluator, one controller
hard exit and one same-workspace resume. No production implementation is patched
except the explicit post-B-evaluation fault seam in the controller subprocess.
"""
import argparse
import base64
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()
PRODUCTION_TREE = '0e73363387123ad067df3cfeff684af7d913fed2'
SLOTS = ('a:0', 'a:1', 'b:0')
MAX_CALLS = 3
MAX_TOKENS = 4096
INTERVENTION = 'CORAL_DIRECTED_INTERVENTION: reflect on the released result before publishing ready.'
SHARED_MARKER = 'CORAL_DIRECTED_SHARED: use the published doubling factor.'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    return json.loads(Path(path).read_bytes())


def seal(path, value):
    """Create an immutable, fsynced JSON record; a replay must match exactly."""
    path = Path(path)
    raw = canonical(value) + b'\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('immutable qualification record changed: ' + path.name)
        return
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def git(*args):
    return subprocess.check_output(['git', '--no-optional-locks', '-C', str(ROOT), *args], text=True).strip()


def plan(model='deepseek-flash'):
    return {'schema': 'proteus.coral.directed.plan.v1', 'production_tree': PRODUCTION_TREE,
        'source_commit': git('rev-parse', 'HEAD'), 'script_sha256': sha(SCRIPT.read_bytes()),
        'model': model, 'endpoint': 'https://api.deepseek.com/chat/completions',
        'workers': ['a', 'b'], 'model_slots': list(SLOTS), 'hard_max_model_dispatches': MAX_CALLS,
        'max_output_tokens_per_request': MAX_TOKENS, 'evaluation_workers': 1,
        'formal_submissions': 2, 'interventions': 1,
        'continuity': 'fresh_fallback, not native model-session continuation',
        'fault': 'os._exit(86) after B GatewayService.process returns, before controller records its result',
        'resume': 'exactly one same-run run_recipe(resume=True); no new model slots',
        'scope': 'Directed protocol qualification on the tiny doubling task; no new circle benchmark or quality claim',
        'unknown_dispatch_policy': 'Consume slot, retain uncertainty, never forward that slot again'}


class Broker:
    """The physical-call cap is enforced here, not inferred from worker reports."""
    def __init__(self, root, *, mode, model, api_key=None):
        self.root = Path(root)
        self.mode, self.model, self.api_key = mode, model, api_key
        self.root.mkdir(parents=True)
        self.permit = secrets.token_urlsafe(32)
        permit_path = self.root / 'permit.txt'
        fd = os.open(permit_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(self.permit)
        self.database = self.root / 'dispatches.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE dispatches(slot TEXT PRIMARY KEY, body_sha TEXT NOT NULL, '
                'status TEXT NOT NULL, provider_id TEXT, response_sha TEXT, input_tokens INTEGER, '
                'output_tokens INTEGER, diagnostic TEXT)')
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    if self.path != '/completion' or self.headers.get('Authorization') != 'Bearer ' + outer.permit:
                        raise PermissionError('invalid broker permit')
                    size = int(self.headers['Content-Length'])
                    if not 0 < size <= 32000:
                        raise ValueError('request size outside declared limit')
                    answer = outer.dispatch(json.loads(self.rfile.read(size)))
                    raw, status = canonical(answer), 200
                except Exception as exc:
                    raw, status = canonical({'error_type': type(exc).__name__}), 409
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port) + '/completion'

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM dispatches ORDER BY slot')]

    def dispatch(self, envelope):
        if set(envelope) != {'slot', 'body'} or envelope['slot'] not in SLOTS:
            raise ValueError('undeclared model slot')
        slot, body = envelope['slot'], envelope['body']
        if (set(body) != {'model', 'temperature', 'max_tokens', 'thinking', 'messages'}
                or body['model'] != self.model or body['max_tokens'] != MAX_TOKENS
                or body['temperature'] != 0 or body['thinking'] != {'type': 'disabled'}
                or len(body['messages']) != 1 or body['messages'][0]['role'] != 'user'):
            raise ValueError('provider request differs from frozen protocol')
        raw = canonical(body)
        if len(raw) > 28000:
            raise ValueError('provider request too large')
        digest = sha(raw)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM dispatches WHERE slot=?', (slot,)).fetchone()
            if old is not None:
                if old['body_sha'] != digest or old['status'] != 'completed':
                    raise ValueError('slot changed or uncertain; do not reforward')
                return load(self.root / (slot.replace(':', '-') + '-response.json'))
            if db.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0] >= MAX_CALLS:
                raise ValueError('physical-call hard cap reached')
            db.execute('INSERT INTO dispatches(slot,body_sha,status) VALUES(?,?,?)', (slot, digest, 'reserved'))
        seal(self.root / (slot.replace(':', '-') + '-request.json'), body)
        try:
            if self.mode == 'fixture':
                factor = 1 if slot == 'a:0' else 2
                answer = {'id': 'fixture-' + slot, 'model': self.model,
                    'choices': [{'message': {'content': json.dumps({'factor': factor, 'note': 'Use factor two for doubling.'})}}],
                    'usage': {'prompt_tokens': 20, 'completion_tokens': 12}}
                response = canonical(answer)
            else:
                request = urllib.request.Request('https://api.deepseek.com/chat/completions', data=raw,
                    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.api_key})
                # Exactly one physical attempt. urllib does not retry this request.
                with urllib.request.urlopen(request, timeout=90) as result:
                    response = result.read(1024 * 1024 + 1)
                if len(response) > 1024 * 1024 or self.api_key.encode() in response:
                    raise ValueError('unsafe or oversized provider response')
                answer = json.loads(response)
            raw_path = self.root / (slot.replace(':', '-') + '-http-response.bin')
            with raw_path.open('xb') as stream:
                stream.write(response)
                stream.flush()
                os.fsync(stream.fileno())
            if type(answer['id']) is not str or not answer['id']:
                raise ValueError('provider receipt has no identity')
            usage = answer['usage']
            if not (type(usage['prompt_tokens']) is int and type(usage['completion_tokens']) is int
                    and 0 <= usage['completion_tokens'] <= MAX_TOKENS):
                raise ValueError('provider usage differs from token cap')
            seal(self.root / (slot.replace(':', '-') + '-response.json'), answer)
            with self.connect() as db:
                db.execute('UPDATE dispatches SET status=?,provider_id=?,response_sha=?,input_tokens=?,output_tokens=? WHERE slot=?',
                    ('completed', answer['id'], sha(response), usage['prompt_tokens'], usage['completion_tokens'], slot))
            return answer
        except BaseException as exc:
            with self.connect() as db:
                db.execute('UPDATE dispatches SET status=?,diagnostic=? WHERE slot=?', ('unknown', type(exc).__name__, slot))
            raise

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def worker(args):
    """Actual OS worker bridge. Stdlib only; receives no DeepSeek credential."""
    launch = json.loads(sys.stdin.readline())
    name = launch['identity']['worker_id']
    workspace = Path.cwd()
    stopped = False
    uncertain = False
    prior = set(workspace.glob('model-*-response.json'))

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    seal(workspace / ('launch-' + sha(canonical(launch)) + '.json'), launch)

    def gateway(path, body=None):
        # Retries are only for the idempotent local gateway, NEVER the model.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            request = urllib.request.Request(os.environ['PROTEUS_EVALUATION_URL'] + path,
                data=None if body is None else canonical(body), headers={
                    'Authorization': 'Bearer ' + os.environ['PROTEUS_EVALUATION_TOKEN'],
                    'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    return json.load(response)
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(.05)
        raise TimeoutError('local gateway did not recover')

    def shared(wait_for=None):
        index = 0
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline and not stopped:
            snap = gateway('/v1/knowledge/snapshots', {'operation_id': f'{name}-read-{sha(canonical(launch))}-{index}'})
            found = []
            for ref in snap['snapshot']['revisions']:
                record = gateway('/v1/knowledge/revisions/' + ref['content_hash'].split(':')[1])
                write = record['revision']['write']
                if write['kind'] != 'notes':
                    continue
                assert record['ref']['content_hash'] == 'sha256:' + sha(canonical(record['revision']))
                text = '\n'.join(base64.b64decode(f['base64']).decode() for f in record['files'])
                seal(workspace / ('knowledge-' + ref['content_hash'].split(':')[1] + '.json'), record)
                found.append({'source': ref, 'writer_id': write['writer_id'], 'key': write['key'], 'text': text})
            if wait_for is None or any(item['key'] == wait_for for item in found):
                return found
            time.sleep(.05)
            index += 1
        if stopped:
            return []
        raise TimeoutError('required publication not observed')

    def model(index, context):
        nonlocal uncertain
        context.update(worker=name, stage=('a_initial' if index == 0 else 'a_intervention') if name == 'a' else 'b_shared',
                       controller_orientation=launch['prompt'], controller_launch_sha256=sha(canonical(launch)))
        prompt = ('Return one JSON object with integer factor and nonempty note. '
            'For a_initial return factor 1; for a_intervention and b_shared return factor 2. '
            'This is a directed protocol check, not a benchmark. Read the actual controller and shared material below.\n'
            + canonical(context).decode())
        body = {'model': args.model, 'temperature': 0, 'max_tokens': MAX_TOKENS,
                'thinking': {'type': 'disabled'}, 'messages': [{'role': 'user', 'content': prompt}]}
        stem = 'model-' + str(index)
        intent, response_path = workspace / (stem + '-intent.json'), workspace / (stem + '-response.json')
        seal(workspace / (stem + '-request.json'), body)
        if not response_path.exists():
            if intent.exists():
                uncertain = True
                raise ValueError('old dispatch has no sealed response; never resend')
            seal(intent, {'slot': name + ':' + str(index), 'body_sha256': sha(canonical(body)),
                'launch_sha256': sha(canonical(launch)), 'shared_sources': [item['source'] for item in context['shared']]})
            request = urllib.request.Request(args.broker, data=canonical({'slot': name + ':' + str(index), 'body': body}),
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + Path(args.permit).read_text()})
            try:
                with urllib.request.urlopen(request, timeout=100) as result:
                    answer = json.load(result)
                seal(response_path, answer)
            except BaseException:
                uncertain = True
                raise
        answer = load(response_path)
        value = json.loads(answer['choices'][0]['message']['content'])
        expected = 1 if name == 'a' and index == 0 else 2
        if type(value.get('factor')) is not int or value['factor'] != expected or not isinstance(value.get('note'), str) or not value['note'].strip():
            raise ValueError('model did not satisfy bounded output contract; no retry')
        return value

    def publish(key, text, refs=()):
        files = [{'path': 'finding.txt', 'base64': base64.b64encode(text.encode()).decode(), 'executable': False}]
        outcome = gateway('/v1/knowledge/writes', {'action': 'publish', 'operation_id': key,
            'kind': 'notes', 'key': key, 'files': files, 'sources': list(refs)})
        seal(workspace / (key + '-published.json'), outcome)
        return outcome['outcome']['revision']

    def submit(factor):
        # Only the validated integer enters this trusted, fixed source template.
        source = "import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({'output':r['task']['input']*" + str(factor) + ",'state':r['state']}))\n"
        ticket = gateway('/v1/submissions', {'operation_id': name + '-proposal', 'evaluation_id': 'validation',
            'files': [{'path': 'main.py', 'base64': base64.b64encode(source.encode()).decode(), 'executable': False}]})
        seal(workspace / 'ticket.json', ticket)
        while not stopped:
            result = gateway('/v1/submissions/' + ticket['ticket_id'])
            if result['status'] in ('completed', 'failed'):
                seal(workspace / 'formal.json', result)
                return
            time.sleep(.025)

    try:
        if name == 'a' and launch['continuity'] == 'fresh':
            value = model(0, {'shared': []})
            publish('a-strategy', SHARED_MARKER + '\n' + value['note'])
            submit(value['factor'])
        elif name == 'a':
            if launch['continuity'] != 'fresh_fallback' or INTERVENTION not in launch['prompt']:
                raise ValueError('actual controller intervention missing from generation')
            evidence = shared('a-strategy')
            value = model(1, {'shared': evidence})
            publish('a-intervention-ready', value['note'], [item['source'] for item in evidence])
        elif name == 'b':
            evidence = shared('a-intervention-ready')
            if not stopped:
                assert any(item['writer_id'] == 'a' and SHARED_MARKER in item['text'] for item in evidence)
                value = model(0, {'shared': evidence})
                publish('b-consumed', value['note'], [item['source'] for item in evidence])
                submit(value['factor'])
        else:
            raise ValueError('unknown worker')
        while not stopped:
            time.sleep(.025)
    finally:
        new = [load(p)['usage'] for p in workspace.glob('model-*-response.json') if p not in prior]
        print(json.dumps({'event': 'result', 'usage': {'model_calls': None if uncertain else len(new),
            'input_tokens': None if uncertain else sum(u['prompt_tokens'] for u in new),
            'output_tokens': None if uncertain else sum(u['completion_tokens'] for u in new),
            'cost_usd': None}}), flush=True)


def production_imports():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def prepare(root, broker):
    production_imports()
    from proteus.core.contracts import BudgetLimits
    from proteus.examples.coral_search import prepare as example_prepare, register
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.cli import source_pin
    from proteus.recipes.config import ReferenceSpec

    path = example_prepare(root / 'study', workers=2)
    recipe = load_recipe(path)
    command = (sys.executable, str(SCRIPT), 'worker', '--broker', broker.url,
        '--permit', str(broker.root / 'permit.txt'), '--model', broker.model)
    pins = {name: 'sha256:' + sha(Path(name).read_bytes()) for name in command[:2]}
    runtime = replace(recipe.workers[0].runtime, options={'command': command, 'command_files': pins,
        'credential_env': (), 'timeout_seconds': 600, 'shutdown_grace_seconds': 5})
    policy = recipe.event_policies[0]
    policy = replace(policy, resume_policy='fresh',
        fresh_orientation='Fresh generation: retain the same workspace and read the actual shared publications.',
        operator=replace(policy.operator, options={'rules': [{'name': 'reflect', 'trigger': 'evaluations',
            'every': 1, 'prompt': INTERVENTION}]}))
    evaluations = tuple(replace(e, tasks=replace(e.tasks, task_ids=e.tasks.task_ids[:1])) for e in recipe.evaluations)
    recipe = replace(recipe, name='directed-coral-protocol-' + broker.mode,
        workers=tuple(replace(w, runtime=runtime) for w in recipe.workers),
        event_policies=(policy,), evaluations=evaluations,
        method=replace(recipe.method, options={**recipe.method.options, 'max_submissions': 2,
            'max_restarts_per_worker': 0, 'max_polls': 9000, 'poll_seconds': .05,
            'bootstrap_prompt': 'Perform the bounded directed protocol; no private task access.'}),
        execution=replace(recipe.execution, max_workers=1),
        budget=BudgetLimits(max_model_calls=MAX_CALLS, max_evaluations=4, max_concurrency=3),
        reference=ReferenceSpec(profile='adapted', status='experimental',
            deviations=('Directed tiny-task current-source protocol qualification, not circle optimization.',
                        'Two real command workers; fixture transport in dry-run and bounded live model only after approval.',
                        'Fixed reviewed source template parameterized only by a validated integer; no model code execution.',
                        'Fresh-context fallback, not native model-session continuation.')))
    registry = builtin_registry()
    register(registry)
    recipe = registry.resolve(recipe)
    lock = registry.make_lock(recipe, [source_pin('proteus', ROOT)])
    seal(root / 'recipe.json', recipe.to_dict())
    seal(root / 'lock.json', lock.to_dict())
    return recipe, lock, registry


def ledger_projection(ledger):
    return [{'id': row['id'], 'state': row['state'],
        'kind': row['identity']['metadata'].get('kind'),
        'operation_id': row['identity']['metadata'].get('operation_id'),
        'identity_sha256': sha(canonical(row['identity'])),
        'settlement_sha256': sha(canonical(row['settlement'])) if row['settlement'] is not None else None}
        for row in ledger.entries()]


def controller(args):
    production_imports()
    from proteus.examples.coral_search import register
    from proteus.gateway.service import GatewayService
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.manifest import DependencyLock
    from proteus.recipes.runner import run_recipe

    root = Path(args.out)
    recipe = load_recipe(root / 'recipe.json')
    lock = DependencyLock.from_json((root / 'lock.json').read_text())
    registry = builtin_registry()
    register(registry)
    if not args.resume:
        original = GatewayService.process

        def fault(self, ticket, **kwargs):
            result = original(self, ticket, **kwargs)
            if ticket.worker_id == 'b':
                with sqlite3.connect(root / 'broker/dispatches.sqlite3') as db:
                    db.row_factory = sqlite3.Row
                    rows = [dict(r) for r in db.execute('SELECT * FROM dispatches ORDER BY slot')]
                assert len(rows) == MAX_CALLS and all(r['status'] == 'completed' for r in rows)
                seal(root / 'controller-hard-exit.json', {'controller_pid': os.getpid(), 'exit_code': 86,
                    'boundary': 'after_B_gateway_evaluation_settled_before_controller_result_publication',
                    'ticket': ticket.to_dict(), 'gateway_result_sha256': sha(canonical(result.to_dict())),
                    'ledger_before': ledger_projection(self.services.ledger),
                    'balance_before': self.services.ledger.balance(), 'broker_rows_before': rows})
                os._exit(86)
            return result

        GatewayService.process = fault
    result = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run',
        artifact_source=root / 'study/artifact-store', resume=args.resume)
    seal(root / ('resume-result.json' if args.resume else 'unexpected-first-result.json'), result.to_dict())
    if result.status.value != 'completed':
        raise RuntimeError('CORAL did not complete; inspect retained diagnostics')


def audit(root, broker):
    """Independent request-content checks on top of the production saved-run reader."""
    production_imports()
    from proteus.core.methods import RunServices
    from proteus.core.serialization import content_hash
    from proteus.examples.coral_search import register
    from proteus.methods.coral.report import report_coral
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.manifest import DependencyLock

    recipe = load_recipe(root / 'recipe.json')
    lock = DependencyLock.from_json((root / 'lock.json').read_text())
    report = report_coral(root / 'run', include_private=True)
    seal(root / 'private-cooperation-report.json', report)
    assert report['stop_reason'] == 'submission_limit' and len(report['submissions']) == 2
    assert len(report['events']['interventions']) == 1
    assert all(w['restarts'] == 0 for w in report['workers'])
    assert {w['worker_id']: w['generations'] for w in report['workers']} == {'a': 2, 'b': 1}
    assert recipe.execution.max_workers == 1 and recipe.budget.max_model_calls == MAX_CALLS
    launches = {}
    for path in (root / 'run/private/workers').glob('*/*/request.json'):
        value = load(path)
        launches['sha256:' + sha(canonical(value))] = value
    rows = broker.rows()
    assert len(rows) == MAX_CALLS and {r['slot'] for r in rows} == set(SLOTS)
    assert all(r['status'] == 'completed' for r in rows)
    assert len({r['provider_id'] for r in rows}) == MAX_CALLS
    published = {r['revision_hash']: r for r in report['knowledge']['revisions']}
    bindings = []
    for row in rows:
        logical_worker, index = row['slot'].split(':')
        candidates = []
        for workspace in (root / 'run/workers').glob('*/workspace'):
            intent_path = workspace / ('model-' + index + '-intent.json')
            if intent_path.exists():
                intent = load(intent_path)
                if intent['slot'] == row['slot']:
                    candidates.append((workspace, intent))
        assert len(candidates) == 1
        workspace, intent = candidates[0]
        body = load(workspace / ('model-' + index + '-request.json'))
        assert sha(canonical(body)) == intent['body_sha256'] == row['body_sha']
        assert canonical(body) == canonical(load(broker.root / (row['slot'].replace(':', '-') + '-request.json')))
        answer = load(workspace / ('model-' + index + '-response.json'))
        assert answer == load(broker.root / (row['slot'].replace(':', '-') + '-response.json'))
        assert answer['id'] == row['provider_id']
        assert sha((broker.root / (row['slot'].replace(':', '-') + '-http-response.bin')).read_bytes()) == row['response_sha']
        assert body['max_tokens'] == MAX_TOKENS
        context = json.loads(body['messages'][0]['content'].split('\n', 1)[1])
        launch = launches['sha256:' + context['controller_launch_sha256']]
        assert launch['identity']['worker_id'] == logical_worker
        assert context['controller_orientation'] == launch['prompt']
        assert intent['launch_sha256'] == context['controller_launch_sha256']
        assert intent['shared_sources'] == [item['source'] for item in context['shared']]
        sources = []
        for item in context['shared']:
            ref = item['source']
            record = load(workspace / ('knowledge-' + ref['content_hash'].split(':')[1] + '.json'))
            assert record['ref'] == ref and 'sha256:' + sha(canonical(record['revision'])) == ref['content_hash']
            write = record['revision']['write']
            assert published[ref['content_hash']]['writer_id'] == item['writer_id'] == write['writer_id']
            assert published[ref['content_hash']]['key'] == item['key'] == write['key']
            assert item['text'] == '\n'.join(base64.b64decode(f['base64']).decode() for f in record['files'])
            sources.append({'source_hash': ref['content_hash'], 'writer_id': item['writer_id'],
                'content_sha256': sha(item['text'].encode())})
        bindings.append({'slot': row['slot'], 'body_sha256': row['body_sha'],
            'provider_id': row['provider_id'], 'response_sha256': row['response_sha'],
            'launch_hash': 'sha256:' + context['controller_launch_sha256'], 'sources': sources})
        if row['slot'] == 'b:0':
            assert any(item['writer_id'] == 'a' and SHARED_MARKER in item['text'] for item in context['shared'])
        if row['slot'] == 'a:1':
            intervention = report['events']['interventions'][0]
            assert intervention['worker_id'] == 'a' and intervention['request_hash'] == 'sha256:' + context['controller_launch_sha256']
            assert INTERVENTION in context['controller_orientation'] and launch['continuity'] == 'fresh_fallback'
    boundary = load(root / 'controller-hard-exit.json')
    assert boundary['broker_rows_before'] == rows, 'No new model dispatch or settlement is allowed after controller restart'
    registry = builtin_registry()
    register(registry)
    services = RunServices(run_id='run', recipe=recipe, registry=registry, root=root / 'run', lock_hash=content_hash(lock))
    after = ledger_projection(services.ledger)
    before_by_id = {r['id']: r for r in boundary['ledger_before']}
    after_by_id = {r['id']: r for r in after}
    for identity, previous in before_by_id.items():
        assert identity in after_by_id and previous['identity_sha256'] == after_by_id[identity]['identity_sha256']
        if previous['settlement_sha256'] is not None:
            assert previous['settlement_sha256'] == after_by_id[identity]['settlement_sha256']
    subject_before = {r['id'] for r in boundary['ledger_before'] if r['kind'] == 'subject'}
    subject_after = {r['id'] for r in after if r['kind'] == 'subject'}
    # A separate final hidden confirmation adds exactly one new subject trial.
    assert len(subject_before) == 3 and len(subject_after) == 4 and len(subject_after - subject_before) == 1
    events = services.ledger.events()
    settled = [e['entry'] for e in events if e['kind'] == 'settled']
    assert len(settled) == len(set(settled))
    assert services.ledger.balance()['active_slots'] == 0
    assert load(root / 'resume-result.json')['status'] == 'completed'
    assert git('rev-parse', 'HEAD:proteus') == PRODUCTION_TREE
    return {'schema': 'proteus.coral.directed.request-level-acceptance.v1',
        'mode': broker.mode, 'production_tree': PRODUCTION_TREE, 'plan_sha256': sha(canonical(load(root / 'plan.json'))),
        'source_commit': git('rev-parse', 'HEAD'), 'script_sha256': sha(SCRIPT.read_bytes()),
        'model_transport_dispatches': MAX_CALLS,
        'real_paid_provider_dispatches': MAX_CALLS if broker.mode == 'live' else 0,
        'simulated_model_usage_is_NOT_paid_usage': broker.mode == 'fixture',
        'all_model_requests_max_output_tokens': MAX_TOKENS, 'model_unknown_rows': 0,
        'cross_worker_publication_in_actual_B_request': True,
        'one_intervention_in_actual_A_next_generation_request': True,
        'controller_actual_hard_exit_code': 86, 'same_workspace_resume_count': 1,
        'sealed_responses_and_physical_dispatches_unchanged_after_resume': True,
        'already_settled_evaluation_identities_and_settlements_preserved': True,
        'no_duplicate_native_settlement': True, 'evaluation_workers': 1,
        'native_cost_unknowns_unchanged': services.ledger.balance(),
        'request_bindings': bindings, 'native_report_hash': report['report_hash'],
        'not_historical_v5_1_or_benchmark_or_native_model_session_evidence': True}


def cleanup(root, recipe, lock, registry):
    if not (root / 'run/manifest.json').exists():
        return
    from proteus.core.methods import RunServices
    from proteus.core.serialization import content_hash
    services = RunServices(run_id='run', recipe=recipe, registry=registry, root=root / 'run', lock_hash=content_hash(lock))
    for name in ('a', 'b'):
        worker_service = services.worker(name)
        head = worker_service.controller.head()
        if head is not None and worker_service.poll(head).completion is None:
            worker_service.controller.control(head, 'stop')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if all((s.controller.head() is None or s.poll(s.controller.head()).completion is not None)
               for s in (services.worker('a'), services.worker('b'))):
            return
        time.sleep(.05)
    seal(root / 'cleanup-incomplete.json', {'status': 'owned workers not confirmed terminal; do not restart'})


def run(args):
    root = Path(args.out).resolve()
    if root.exists():
        raise ValueError('Qualification root already exists; never retry or overwrite')
    if git('rev-parse', 'HEAD:proteus') != PRODUCTION_TREE or git('diff', 'HEAD', '--', 'proteus'):
        raise ValueError('Production source differs from reviewed tree')
    configuration = plan(args.model)
    api_key = None
    if args.mode == 'live':
        if not args.execute_approved_live or args.approval is None:
            raise ValueError('Live mode requires an explicit reviewed approval; fixture is the default')
        approval = load(args.approval)
        if (approval.get('approved') is not True or approval.get('plan_sha256') != sha(canonical(configuration))
                or approval.get('max_physical_dispatches') != MAX_CALLS):
            raise ValueError('Live approval does not bind this exact plan')
        api_key = os.environ.get('DEEPSEEK_API_KEY')
        if not api_key:
            raise ValueError('Approved live mode requires an existing DEEPSEEK_API_KEY environment credential')
    root.mkdir(parents=True)
    seal(root / 'plan.json', configuration)
    seal(root / 'exclusive-start-claim.json', {'mode': args.mode, 'plan_sha256': sha(canonical(configuration))})
    broker = Broker(root / 'broker', mode=args.mode, model=args.model, api_key=api_key)
    broker.thread.start()
    recipe = lock = registry = None
    try:
        recipe, lock, registry = prepare(root, broker)
        environment = {name: os.environ[name] for name in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if name in os.environ}
        argv = [sys.executable, str(SCRIPT), 'controller', '--out', str(root)]
        with (root / 'first-controller.log').open('xb') as stream:
            first = subprocess.run(argv, env=environment, stdout=stream, stderr=subprocess.STDOUT, timeout=500)
        if first.returncode != 86 or not (root / 'controller-hard-exit.json').is_file():
            raise ValueError('Expected actual controller hard-exit boundary was not reached')
        seal(root / 'single-resume-claim.json', {'same_workspace': str(root / 'run'), 'first_return_code': 86})
        with (root / 'resume-controller.log').open('xb') as stream:
            second = subprocess.run([*argv, '--resume'], env=environment,
                stdout=stream, stderr=subprocess.STDOUT, timeout=100)
        if second.returncode != 0:
            raise ValueError('Same-workspace resume failed; retain evidence and do not retry')
        result = audit(root, broker)
        seal(root / 'acceptance.json', result)
        print(json.dumps({'evidence': str(root / 'acceptance.json'), 'mode': args.mode,
            'model_transport_dispatches': result['model_transport_dispatches'],
            'real_paid_provider_dispatches': result['real_paid_provider_dispatches'],
            'request_level_checks_passed': True}))
    except BaseException as exc:
        seal(root / 'failure.json', {'error_type': type(exc).__name__, 'message_sha256': sha(str(exc).encode()),
            'broker_rows': broker.rows(), 'paid_retry_or_unknown_resend_permitted': False})
        raise
    finally:
        if recipe is not None:
            cleanup(root, recipe, lock, registry)
        broker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_plan = commands.add_parser('plan')
    prepare_plan.add_argument('--model', default='deepseek-flash')
    for command in ('run', 'controller'):
        child = commands.add_parser(command)
        child.add_argument('--out', type=Path, required=True)
        if command == 'run':
            child.add_argument('--mode', choices=('fixture', 'live'), default='fixture')
            child.add_argument('--model', default='deepseek-flash')
            child.add_argument('--approval', type=Path)
            child.add_argument('--execute-approved-live', action='store_true')
        else:
            child.add_argument('--resume', action='store_true')
    child = commands.add_parser('worker')
    child.add_argument('--broker', required=True)
    child.add_argument('--permit', required=True)
    child.add_argument('--model', required=True)
    args = parser.parse_args()
    if args.command == 'plan':
        value = plan(args.model)
        print(json.dumps({'plan': value, 'plan_sha256': sha(canonical(value))}, indent=2))
    elif args.command == 'worker':
        worker(args)
    elif args.command == 'controller':
        controller(args)
    else:
        run(args)


if __name__ == '__main__':
    main()
