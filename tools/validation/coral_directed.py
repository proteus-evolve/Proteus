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
import re
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


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


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


def plan_value(model, mode, pin, script_hash):
    if mode not in ('fixture', 'live') or not isinstance(model, str) or not model.strip():
        raise ValueError('invalid declared transport mode or model')
    return {'schema': 'proteus.coral.directed.plan.v1', 'production_tree': PRODUCTION_TREE,
        'source_commit': pin['revision'], 'source_pin': pin, 'script_sha256': script_hash, 'transport_mode': mode,
        'model': model, 'endpoint': 'https://api.deepseek.com/chat/completions',
        'workers': ['a', 'b'], 'model_slots': list(SLOTS), 'hard_max_model_dispatches': MAX_CALLS,
        'max_output_tokens_per_request': MAX_TOKENS, 'evaluation_workers': 1,
        'formal_submissions': 2, 'interventions': 1,
        'continuity': 'fresh_fallback, not native model-session continuation',
        'fault': 'os._exit(86) after B GatewayService.process returns, before controller records its result',
        'resume': 'exactly one same-run run_recipe(resume=True); no new model slots',
        'scope': 'Directed protocol qualification on the tiny doubling task; no new circle benchmark or quality claim',
        'unknown_dispatch_policy': 'Consume slot, retain uncertainty, never forward that slot again'}


def plan(model='deepseek-flash', mode='fixture'):
    production_imports()
    from proteus.recipes.cli import source_pin
    return plan_value(model, mode, source_pin('proteus', ROOT).to_dict(), sha(SCRIPT.read_bytes()))


class Broker:
    """The physical-call cap is enforced here, not inferred from worker reports."""
    def __init__(self, root, *, mode, model, plan_sha256='unit', api_key=None):
        self.root = Path(root)
        self.mode, self.model, self.api_key = mode, model, api_key
        self.plan_sha256 = plan_sha256
        self.root.mkdir(parents=True)
        seal(self.root / 'transport-identity.json', {'mode': mode, 'model': model, 'plan_sha256': plan_sha256,
            'endpoint': 'https://api.deepseek.com/chat/completions' if mode == 'live' else 'deterministic-fixture'})
        self.permit = secrets.token_urlsafe(32)
        permit_path = self.root / 'permit.txt'
        fd = os.open(permit_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(self.permit)
        self.database = self.root / 'dispatches.sqlite3'
        self.event_lock = threading.Lock()
        self.worker_events = []
        self.started = {}
        self.b_waiting = threading.Event()
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
                    if self.headers.get('Authorization') != 'Bearer ' + outer.permit:
                        raise PermissionError('invalid broker permit')
                    size = int(self.headers['Content-Length'])
                    if not 0 < size <= 32000:
                        raise ValueError('request size outside declared limit')
                    value = json.loads(self.rfile.read(size))
                    if self.path == '/completion':
                        answer = outer.dispatch(value)
                    elif self.path in ('/started', '/waiting', '/published'):
                        answer = outer.worker_event(self.path, value)
                    else:
                        raise ValueError('unknown broker endpoint')
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

    def host_process(self, pid):
        if not integer(pid) or pid <= 0:
            raise ValueError('invalid worker process identity')
        raw = subprocess.check_output(['/bin/ps', '-ww', '-p', str(pid), '-o', 'pid=,ppid=,stat=,args='], text=True)
        fields = raw.strip().split(None, 3)
        if len(fields) != 4 or int(fields[0]) != pid or fields[2].startswith('Z') or str(SCRIPT) + ' worker ' not in fields[3]:
            raise ValueError('registered process is not an active qualification worker')
        return {'pid': pid, 'parent_pid': int(fields[1]), 'process_state': fields[2],
                'command_sha256': sha(fields[3].encode())}

    def worker_event(self, kind, value):
        if value['worker'] not in ('a', 'b'):
            raise ValueError('foreign worker observation')
        process = self.host_process(value['pid'])
        with self.event_lock:
            record = {'kind': kind, 'worker': value['worker'], 'input_sha256': value['input_sha256'],
                'observed_monotonic_ns': time.monotonic_ns(), 'host_process': process}
            if kind == '/started':
                self.started[(value['worker'], value['input_sha256'])] = value['pid']
            elif self.started.get((value['worker'], value['input_sha256'])) != value['pid']:
                raise ValueError('observation does not match a registered generation')
            if kind == '/waiting' and value['worker'] == 'b':
                self.b_waiting.set()
            if kind == '/published':
                record['published_ref'] = value['published_ref']
                candidates = [(key, pid) for key, pid in self.started.items() if key[0] == 'b']
                if len(candidates) != 1 or not self.b_waiting.is_set():
                    raise ValueError('B must be active and waiting when A publishes')
                record['other_waiting_worker'] = {'input_sha256': candidates[0][0][1],
                    **self.host_process(candidates[0][1])}
            seal(self.root / ('worker-event-' + str(len(self.worker_events)).zfill(3) + '.json'), record)
            self.worker_events.append(record)
        return {'recorded': True}

    def forward(self, raw):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None
        request = urllib.request.Request('https://api.deepseek.com/chat/completions', data=raw,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.api_key})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=90) as result:
            return result.read(1024 * 1024 + 1), {'network_forwarded': True,
                'transport': 'https', 'status': result.status, 'url': result.geturl()}

    def dispatch(self, envelope):
        if set(envelope) != {'slot', 'body'} or envelope['slot'] not in SLOTS:
            raise ValueError('undeclared model slot')
        slot, body = envelope['slot'], envelope['body']
        if (set(body) != {'model', 'temperature', 'max_tokens', 'thinking', 'messages'}
                or body['model'] != self.model or not integer(body['max_tokens']) or body['max_tokens'] != MAX_TOKENS
                or body['temperature'] != 0 or body['thinking'] != {'type': 'disabled'}
                or len(body['messages']) != 1 or body['messages'][0]['role'] != 'user'):
            raise ValueError('provider request differs from frozen protocol')
        raw = canonical(body)
        if len(raw) > 28000:
            raise ValueError('provider request too large')
        digest = sha(raw)
        context = json.loads(body['messages'][0]['content'].split('\n', 1)[1])
        if (context['transport_mode'] != self.mode or context['qualification_plan_sha256'] != self.plan_sha256
                or context['worker'] != slot.split(':')[0]):
            raise ValueError('transport or plan identity differs from request')
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
        seal(self.root / (slot.replace(':', '-') + '-attempt-intent.json'), {
            'mode': self.mode, 'plan_sha256': self.plan_sha256, 'body_sha256': digest,
            'slot': slot, 'automatic_retry': False})
        forwarding_started = False
        response = None
        transport = None
        try:
            if slot == 'a:0' and not self.b_waiting.wait(timeout=15):
                raise ValueError('second actual worker was not observed waiting')
            if slot == 'a:0':
                a = [(key, pid) for key, pid in self.started.items() if key[0] == 'a']
                b = [(key, pid) for key, pid in self.started.items() if key[0] == 'b']
                if len(a) != 1 or len(b) != 1:
                    raise ValueError('both independent initial generations must be active')
                seal(self.root / 'first-forward-process-witness.json', {
                    'observed_monotonic_ns': time.monotonic_ns(),
                    'a_input_sha256': a[0][0][1], 'a_process': self.host_process(a[0][1]),
                    'b_input_sha256': b[0][0][1], 'b_waiting_process': self.host_process(b[0][1])})
            if self.mode == 'fixture':
                factor = 1 if slot == 'a:0' else 2
                answer = {'id': 'fixture-' + slot, 'model': self.model,
                    'choices': [{'message': {'content': json.dumps({'factor': factor, 'note': 'Use factor two for doubling.'})}}],
                    'usage': {'prompt_tokens': 20, 'completion_tokens': 12}}
                response = canonical(answer)
                transport = {'network_forwarded': False, 'transport': 'fixture', 'status': None, 'url': None}
            else:
                # Exactly one attempt; redirects and automatic retries disabled.
                forwarding_started = True
                response, transport = self.forward(raw)
                if len(response) > 1024 * 1024 or self.api_key.encode() in response:
                    raise ValueError('unsafe or oversized provider response')
                answer = json.loads(response)
            raw_path = self.root / (slot.replace(':', '-') + '-http-response.bin')
            with raw_path.open('xb') as stream:
                stream.write(response)
                stream.flush()
                os.fsync(stream.fileno())
            if not isinstance(answer['id'], str) or not answer['id']:
                raise ValueError('provider receipt has no identity')
            if not isinstance(answer.get('model'), str) or not answer['model']:
                raise ValueError('provider receipt has no resolved model identity')
            usage = answer['usage']
            if not (integer(usage['prompt_tokens']) and usage['prompt_tokens'] >= 0 and integer(usage['completion_tokens'])
                    and 0 <= usage['completion_tokens'] <= MAX_TOKENS):
                raise ValueError('provider usage differs from token cap')
            seal(self.root / (slot.replace(':', '-') + '-response.json'), answer)
            seal(self.root / (slot.replace(':', '-') + '-transport.json'), {'mode': self.mode,
                'model': self.model, 'plan_sha256': self.plan_sha256, 'body_sha256': digest,
                'wire_response_sha256': sha(response), 'normalized_answer_sha256': sha(canonical(answer)),
                'provider_id': answer['id'], 'actual_provider_model': answer['model'], 'usage': usage, **transport})
            with self.connect() as db:
                db.execute('UPDATE dispatches SET status=?,provider_id=?,response_sha=?,input_tokens=?,output_tokens=? WHERE slot=?',
                    ('completed', answer['id'], sha(response), usage['prompt_tokens'], usage['completion_tokens'], slot))
            return answer
        except BaseException as exc:
            if isinstance(exc, urllib.error.HTTPError):
                error_body = exc.read(1024 * 1024 + 1)
                transport = {'network_forwarded': True, 'transport': 'https',
                    'status': exc.code, 'url': exc.geturl()}
                if len(error_body) <= 1024 * 1024 and (not self.api_key or self.api_key.encode() not in error_body):
                    response = error_body
                    error_path = self.root / (slot.replace(':', '-') + '-http-error.bin')
                    with error_path.open('xb') as stream:
                        stream.write(error_body)
                        stream.flush()
                        os.fsync(stream.fileno())
            seal(self.root / (slot.replace(':', '-') + '-failed-attempt.json'), {
                'mode': self.mode, 'slot': slot, 'plan_sha256': self.plan_sha256,
                'body_sha256': digest, 'forwarding_started': forwarding_started,
                'wire_response_sha256_if_captured': sha(response) if response is not None else None,
                'observed_transport_if_available': transport, 'error_type': type(exc).__name__,
                'unknown_is_NOT_zero': True, 'automatic_resend_permitted': False})
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

    def observe(kind, **extra):
        value = {'worker': name, 'pid': os.getpid(), 'input_sha256': sha(canonical(launch)), **extra}
        request = urllib.request.Request(args.broker.rsplit('/', 1)[0] + kind, data=canonical(value),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + Path(args.permit).read_text()})
        with urllib.request.urlopen(request, timeout=20) as response:
            assert json.load(response)['recorded']
        print(json.dumps({'event': 'qualification_process_observation', **value, 'kind': kind}), flush=True)

    observe('/started')

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
                       controller_orientation=launch['prompt'], controller_input_sha256=sha(canonical(launch)),
                       transport_mode=args.transport_mode, qualification_plan_sha256=args.plan_hash)
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
                'controller_input_sha256': sha(canonical(launch)), 'shared_sources': [item['source'] for item in context['shared']]})
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
        if not integer(value.get('factor')) or value['factor'] != expected or not isinstance(value.get('note'), str) or not value['note'].strip():
            raise ValueError('model did not satisfy bounded output contract; no retry')
        return value

    def publish(key, text, refs=()):
        files = [{'path': 'finding.txt', 'base64': base64.b64encode(text.encode()).decode(), 'executable': False}]
        outcome = gateway('/v1/knowledge/writes', {'action': 'publish', 'operation_id': key,
            'kind': 'notes', 'key': key, 'files': files, 'sources': list(refs)})
        seal(workspace / (key + '-published.json'), outcome)
        return outcome['outcome']['revision']

    def submit(factor):
        ticket = gateway('/v1/submissions', {'operation_id': name + '-proposal', 'evaluation_id': 'validation',
            'files': [{'path': 'factor.json', 'base64': base64.b64encode(canonical({'factor': factor})).decode(), 'executable': False}]})
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
            ref = publish('a-strategy', SHARED_MARKER + '\n' + value['note'])
            observe('/published', published_ref=ref)
            submit(value['factor'])
        elif name == 'a':
            if launch['continuity'] != 'fresh_fallback' or INTERVENTION not in launch['prompt']:
                raise ValueError('actual controller intervention missing from generation')
            evidence = shared('a-strategy')
            value = model(1, {'shared': evidence})
            publish('a-intervention-ready', value['note'], [item['source'] for item in evidence])
        elif name == 'b':
            observe('/waiting')
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
    import proteus
    if not Path(proteus.__file__).resolve().is_relative_to(ROOT / 'proteus'):
        raise ValueError('Production package imported from a different checkout')


def source_witness(pin):
    difference = subprocess.check_output(['git', '--no-optional-locks', '-C', str(ROOT), 'diff', 'HEAD', '--binary'])
    names = subprocess.check_output(['git', '--no-optional-locks', '-C', str(ROOT),
        'ls-files', '--others', '--exclude-standard', '-z']).split(b'\0')
    untracked = {}
    for raw in names:
        if raw:
            name = os.fsdecode(raw)
            path = ROOT / name
            data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
            untracked[name] = sha(data)
    return {'source_pin': pin, 'static_git_diff_base64': base64.b64encode(difference).decode(),
            'untracked_static_file_hashes': untracked}


def register_qualification(registry):
    from proteus.core.contracts import Outcome, PluginRef, RuntimeIdentity, Usage
    from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
    from proteus.core.serialization import content_hash
    from proteus.evaluation.records import SubjectResult
    from proteus.examples.coral_search import register
    register(registry)

    class FactorSubject:
        def run(self, context):
            value = load(context.lease.local_root / 'factor.json')
            if set(value) != {'factor'} or not integer(value['factor']) or value['factor'] not in (1, 2):
                raise ValueError('invalid structured factor candidate')
            return SubjectResult(loaded_artifact_hash=context.lease.loaded_artifact_hash,
                runtime=RuntimeIdentity(runtime=context.subject.runner.plugin,
                    environment_hash=content_hash(context.execution.environment)), status=Outcome.SUCCEEDED,
                output=context.task.input * value['factor'], state=context.state,
                usage=Usage(model_calls=0, input_tokens=0, output_tokens=0, cost_usd=0, wall_seconds=0))

    registry.register(Plugin(ref=PluginRef(name='qualification.coral_factor', version='1'), kind='subject',
        options=EmptyOptions, factory=lambda options: FactorSubject(), source='proteus',
        implementation_hash=implementation_hash({'qualification.py': SCRIPT}),
        capabilities=('subject.model_free',)))


def prepare(root, broker):
    production_imports()
    from proteus.core.contracts import BudgetLimits
    from proteus.artifacts.codecs import FileContent
    from proteus.artifacts.store import ArtifactStore
    from proteus.core.contracts import PluginRef
    from proteus.examples.coral_search import prepare as example_prepare
    from proteus.examples.candidate_evaluation import plugin
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.manifest import SourcePin
    from proteus.recipes.config import ReferenceSpec

    path = example_prepare(root / 'study', workers=2)
    recipe = load_recipe(path)
    registry = builtin_registry()
    register_qualification(registry)
    artifact = ArtifactStore(root / 'study/artifact-store', registry).put(
        PluginRef(name='proteus.file_tree', version='1'), {'factor.json': FileContent(canonical({'factor': 1}))})
    command = (sys.executable, str(SCRIPT), 'worker', '--broker', broker.url,
        '--permit', str(broker.root / 'permit.txt'), '--model', broker.model,
        '--transport-mode', broker.mode, '--plan-hash', broker.plan_sha256)
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
        subject=replace(recipe.subject, runner=plugin('qualification.coral_factor'), initial_artifact=artifact),
        method=replace(recipe.method, options={**recipe.method.options, 'max_submissions': 2,
            'max_restarts_per_worker': 0, 'max_polls': 9000, 'poll_seconds': .05,
            'bootstrap_prompt': 'Perform the bounded directed protocol; no private task access.'}),
        execution=replace(recipe.execution, max_workers=1),
        # command workers cannot claim native calls.bounded. The separately
        # audited broker enforces the three physical attempts; native unknown
        # budgets remain null rather than being falsely upgraded to bounded.
        budget=BudgetLimits(max_evaluations=4, max_concurrency=3),
        reference=ReferenceSpec(profile='adapted', status='experimental',
            code=recipe.reference.code, revision=recipe.reference.revision,
            deviations=('Directed tiny-task current-source protocol qualification, not circle optimization.',
                        'Two real command workers; fixture transport in dry-run and bounded live model only after approval.',
                        'Structured integer candidate evaluated by a model-free trusted subject; no generated code execution.',
                        'Fresh-context fallback, not native model-session continuation.')))
    recipe = registry.resolve(recipe)
    lock = registry.make_lock(recipe, [SourcePin.from_dict(load(root / 'plan.json')['source_pin'])])
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


def verify_settlements(events, entries):
    settled = [e['entry'] for e in events if e['kind'] == 'settled']
    assert settled and len(settled) == len(set(settled))
    assert set(settled) == {r['id'] for r in entries if r['settlement'] is not None}
    return len(settled)


def controller(args):
    production_imports()
    from proteus.gateway.service import GatewayService
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.manifest import DependencyLock
    from proteus.recipes.runner import run_recipe

    root = Path(args.out)
    recipe = load_recipe(root / 'recipe.json')
    lock = DependencyLock.from_json((root / 'lock.json').read_text())
    registry = builtin_registry()
    register_qualification(registry)
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
    if not __debug__:
        raise ValueError('Qualification audit must run with assertions enabled')
    production_imports()
    from proteus.core.ledger import Ledger
    from proteus.core.serialization import content_hash
    from proteus.methods.coral.report import report_coral
    from proteus.recipes import load_recipe
    from proteus.recipes.manifest import DependencyLock

    configuration = load(root / 'plan.json')
    frozen = load(root / 'qualification-source.json')
    witness = load(root / 'source-witness.json')
    assert sha(frozen['static_source'].encode()) == frozen['sha256'] == configuration['script_sha256']
    assert frozen['sha256'] == sha(SCRIPT.read_bytes()), 'Use the frozen qualification script, not a relabeled later auditor'
    pin = configuration['source_pin']
    assert configuration == plan_value(configuration['model'], configuration['transport_mode'], pin, frozen['sha256'])
    plan_hash = sha(canonical(configuration))
    claim = load(root / 'exclusive-start-claim.json')
    assert claim['plan_sha256'] == plan_hash
    assert claim['mode'] == configuration['transport_mode'] == broker.mode
    original_root = Path(claim['original_output_root'])
    transport_identity = load(broker.root / 'transport-identity.json')
    assert transport_identity == {'mode': broker.mode, 'model': configuration['model'], 'plan_sha256': plan_hash,
        'endpoint': 'https://api.deepseek.com/chat/completions' if broker.mode == 'live' else 'deterministic-fixture'}
    if broker.mode == 'live':
        approval = load(root / 'live-approval.json')
        expected = {'approved': True, 'plan_sha256': plan_hash, 'max_physical_dispatches': MAX_CALLS,
                    'output_root': str(original_root)}
        assert approval == expected and claim['approval_sha256'] == sha(canonical(approval))
        used = load(root / 'live-approval-used.json')
        assert used == {'plan_sha256': plan_hash, 'output_root': str(original_root)}
        assert claim['used_claim_sha256'] == sha(canonical(used))
    else:
        assert not (root / 'live-approval.json').exists()
    assert witness['source_pin'] == pin and configuration['source_commit'] == pin['revision']
    assert git('rev-parse', pin['revision'] + ':proteus') == configuration['production_tree'] == PRODUCTION_TREE
    difference = base64.b64decode(witness['static_git_diff_base64'])
    untracked = witness['untracked_static_file_hashes']
    dirty = content_hash({'diff': sha(difference), 'untracked': untracked}) if difference or untracked else None
    assert pin['dirty_patch_hash'] == dirty
    if dirty is None:
        blob = subprocess.check_output(['git', '--no-optional-locks', '-C', str(ROOT),
            'show', pin['revision'] + ':' + SCRIPT.relative_to(ROOT).as_posix()])
        assert sha(blob) == frozen['sha256']
    if broker.mode == 'live':
        assert dirty is None, 'Paid qualification requires a clean, frozen source commit'
    recipe = load_recipe(root / 'recipe.json')
    lock = DependencyLock.from_json((root / 'lock.json').read_text())
    assert [s.to_dict() for s in lock.sources if s.name == 'proteus'] == [pin]
    manifest = load(root / 'run/manifest.json')
    assert manifest['recipe'] == recipe.to_dict() and manifest['lock'] == lock.to_dict()
    for spec in recipe.workers:
        command = spec.runtime.options['command']
        assert command[command.index('--model') + 1] == configuration['model']
        assert command[command.index('--transport-mode') + 1] == broker.mode
        assert command[command.index('--plan-hash') + 1] == plan_hash
    report = report_coral(root / 'run', include_private=True)
    assert report['stop_reason'] == 'submission_limit' and len(report['submissions']) == 2
    assert [item['score'] for item in report['submissions']] == [0, 1]
    assert len(report['events']['interventions']) == 1
    assert all(w['restarts'] == 0 for w in report['workers'])
    assert {w['worker_id']: w['generations'] for w in report['workers']} == {'a': 2, 'b': 1}
    assert recipe.execution.max_workers == 1 and recipe.budget.max_model_calls is None
    launches = {}
    inputs = {}
    generation_directories = {}
    for path in (root / 'run/private/workers').glob('*/*/request.json'):
        value = load(path)
        launch_hash = 'sha256:' + sha(canonical(value))
        launches[launch_hash] = value
        generation_directories[launch_hash] = path.parent
        stdin = load(path.parent / 'input.json')
        assert stdin == {key: value[key] for key in ('identity', 'operation_id', 'prompt', 'session_id',
            'continuity', 'fallback_reason', 'session_usage')}
        inputs[sha(canonical(stdin))] = launch_hash
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
        assert body['model'] == configuration['model']
        wire = (broker.root / (row['slot'].replace(':', '-') + '-http-response.bin')).read_bytes()
        assert json.loads(wire) == answer
        assert row['input_tokens'] == answer['usage']['prompt_tokens'] >= 0
        assert integer(answer['usage']['prompt_tokens']) and integer(answer['usage']['completion_tokens'])
        assert row['output_tokens'] == answer['usage']['completion_tokens'] and 0 <= row['output_tokens'] <= MAX_TOKENS
        transport = load(broker.root / (row['slot'].replace(':', '-') + '-transport.json'))
        assert transport['mode'] == broker.mode and transport['model'] == configuration['model']
        assert transport['plan_sha256'] == plan_hash and transport['body_sha256'] == row['body_sha']
        assert transport['wire_response_sha256'] == row['response_sha']
        assert transport['normalized_answer_sha256'] == sha(canonical(answer))
        assert transport['provider_id'] == answer['id'] and transport['usage'] == answer['usage']
        assert transport['actual_provider_model'] == answer['model']
        if broker.mode == 'live':
            assert transport['network_forwarded'] is True and transport['transport'] == 'https'
            assert transport['status'] == 200 and transport['url'] == configuration['endpoint']
            assert not answer['id'].startswith('fixture-')
        else:
            assert transport['network_forwarded'] is False and transport['transport'] == 'fixture'
        context = json.loads(body['messages'][0]['content'].split('\n', 1)[1])
        assert context['transport_mode'] == broker.mode and context['qualification_plan_sha256'] == plan_hash
        launch_hash = inputs[context['controller_input_sha256']]
        launch = launches[launch_hash]
        assert launch['identity']['worker_id'] == logical_worker
        assert context['controller_orientation'] == launch['prompt']
        assert intent['controller_input_sha256'] == context['controller_input_sha256']
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
            components = {c['path']: c for c in write['artifact']['components']}
            assert set(components) == {f['path'] for f in record['files']}
            for file in record['files']:
                data = base64.b64decode(file['base64'])
                component = components[file['path']]
                assert component['content_hash'] == 'sha256:' + sha(data) and component['size_bytes'] == len(data)
                assert component['executable'] == file['executable']
                blob = root / 'run/private/knowledge' / content_hash('research').split(':')[1] / 'artifacts/blobs' / component['content_hash'].split(':')[1]
                assert blob.read_bytes() == data, 'Cached publication content differs from original native artifact blob'
            sources.append({'source_hash': ref['content_hash'], 'writer_id': item['writer_id'],
                'content_sha256': sha(item['text'].encode())})
        bindings.append({'slot': row['slot'], 'worker': logical_worker, 'phase': context['stage'], 'body_sha256': row['body_sha'],
            'requested_model': body['model'], 'actual_provider_model': answer['model'],
            'provider_id': row['provider_id'], 'response_sha256': row['response_sha'],
            'launch_hash': launch_hash, 'supervisor_input_sha256': context['controller_input_sha256'], 'sources': sources,
            'input_tokens': row['input_tokens'], 'output_tokens': row['output_tokens']})
        if row['slot'] == 'b:0':
            assert any(item['writer_id'] == 'a' and SHARED_MARKER in item['text'] for item in context['shared'])
        if row['slot'] == 'a:1':
            intervention = report['events']['interventions'][0]
            assert intervention['worker_id'] == 'a' and intervention['request_hash'] == launch_hash
            assert INTERVENTION in context['controller_orientation'] and launch['continuity'] == 'fresh_fallback'
    boundary = load(root / 'controller-hard-exit.json')
    observations = load(root / 'controller-process-observations.json')
    assert observations['first']['pid'] == boundary['controller_pid']
    assert observations['first']['return_code'] == boundary['exit_code'] == 86
    assert observations['resumed']['return_code'] == 0 and observations['first']['pid'] != observations['resumed']['pid']
    assert load(root / 'single-resume-claim.json') == {'same_workspace': str(original_root / 'run'), 'first_return_code': 86}
    assert boundary['broker_rows_before'] == rows, 'No new model dispatch or settlement is allowed after controller restart'
    with sqlite3.connect((root / 'run/private/ledger.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        entries = [{'id': row['id'], 'state': row['state'], 'identity': json.loads(row['identity_json']),
            'settlement': json.loads(row['settlement_json']) if row['settlement_json'] is not None else None}
            for row in db.execute('SELECT * FROM entries ORDER BY id')]
        events = [dict(r) for r in db.execute('SELECT entry,kind FROM events ORDER BY sequence')]
        charged, unknown, slots, count = Ledger._scan_balance(db, 'run')
    balance = {'charged': {key: None if value is None else float(value) for key, value in charged.items()},
        'unknown': unknown, 'active_slots': slots, 'entries': count}
    after = [{'id': r['id'], 'state': r['state'], 'kind': r['identity']['metadata'].get('kind'),
        'identity_sha256': sha(canonical(r['identity'])),
        'settlement_sha256': sha(canonical(r['settlement'])) if r['settlement'] is not None else None} for r in entries]
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
    verify_settlements(events, entries)
    assert balance['active_slots'] == 0
    assert load(root / 'resume-result.json')['status'] == 'completed'
    observed = [load(path) for path in sorted(broker.root.glob('worker-event-*.json'))]
    publications = [event for event in observed if event['kind'] == '/published']
    assert len(publications) == 1
    publication = publications[0]
    assert publication['worker'] == 'a' and publication['host_process']['pid'] != publication['other_waiting_worker']['pid']
    waiting = [e for e in observed if e['kind'] == '/waiting' and e['worker'] == 'b']
    assert len(waiting) == 1 and waiting[0]['observed_monotonic_ns'] < publication['observed_monotonic_ns']
    first_forward = load(broker.root / 'first-forward-process-witness.json')
    assert waiting[0]['observed_monotonic_ns'] <= first_forward['observed_monotonic_ns'] < publication['observed_monotonic_ns']
    assert first_forward['b_waiting_process']['pid'] == waiting[0]['host_process']['pid'] == publication['other_waiting_worker']['pid']
    assert first_forward['b_input_sha256'] == waiting[0]['input_sha256'] == publication['other_waiting_worker']['input_sha256']
    assert first_forward['a_process']['pid'] == publication['host_process']['pid']
    for event in (publication, waiting[0]):
        launch_hash = inputs[event['input_sha256']]
        stdout = (generation_directories[launch_hash] / 'stdout.log').read_text()
        emitted = [json.loads(line) for line in stdout.splitlines() if line.startswith('{')]
        assert any(row.get('event') == 'qualification_process_observation'
            and row.get('pid') == event['host_process']['pid']
            and row.get('input_sha256') == event['input_sha256']
            and row.get('kind') == event['kind'] for row in emitted)
    assert publication['published_ref']['content_hash'] in published
    return {'schema': 'proteus.coral.directed.request-level-acceptance.v1',
        'mode': broker.mode, 'production_tree': PRODUCTION_TREE, 'plan_sha256': sha(canonical(load(root / 'plan.json'))),
        'source_commit': configuration['source_commit'], 'script_sha256': frozen['sha256'],
        'frozen_source_pin_matches_manifest_lock_and_static_source': True,
        'dirty_source_nonfinal_fixture_only': dirty is not None,
        'model_transport_dispatches': MAX_CALLS,
        'real_paid_provider_dispatches': MAX_CALLS if broker.mode == 'live' else 0,
        'simulated_model_usage_is_NOT_paid_usage': broker.mode == 'fixture',
        'all_model_requests_max_output_tokens': MAX_TOKENS, 'model_unknown_rows': 0,
        'native_command_worker_model_budget_NOT_falsely_claimed_bounded': True,
        'physical_attempt_cap_enforced_by_durable_broker': MAX_CALLS,
        'cross_worker_publication_in_actual_B_request': True,
        'one_intervention_in_actual_A_next_generation_request': True,
        'controller_actual_hard_exit_code': 86, 'same_workspace_resume_count': 1,
        'sealed_responses_and_physical_dispatches_unchanged_after_resume': True,
        'already_settled_evaluation_identities_and_settlements_preserved': True,
        'no_duplicate_native_settlement': True, 'evaluation_workers': 1,
        'native_cost_unknowns_unchanged': balance,
        'actual_two_process_async_overlap_at_A_publication': publication,
        'actual_two_process_overlap_before_A_first_forward': first_forward,
        'cached_shared_files_match_native_knowledge_artifact_blobs': True,
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


def wait_owned(child, timeout):
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()  # This Popen child is still owned/unreaped, never a stored PID.
        child.wait(timeout=10)
        raise


def export_audit_copy(root, destination):
    """Credential-free audit copy, never a resumable replacement of the original.

    Preserve original failed/unknown rows. Do not copy broker permits or select
    gateway credential values. Rebuild only the gateway DB in the copy, with
    exact original noncredential tables and an explicitly empty credential table.
    All other original evidence is copied byte-for-byte or transactionally backed
    up for SQLite; raw provider bodies are retained only if safe for this toy task.
    """
    root, destination = root.resolve(), destination.resolve()
    if destination.exists() or destination.is_relative_to(root):
        raise ValueError('Export requires a fresh destination outside the original run')
    destination.mkdir(parents=True)
    records, excluded, failures = [], [], []
    schemas = {
        'identity': 'CREATE TABLE identity (policy TEXT NOT NULL)',
        'requests': 'CREATE TABLE requests (sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, worker TEXT NOT NULL, operation TEXT NOT NULL, ticket TEXT NOT NULL, status TEXT NOT NULL, result TEXT, UNIQUE(worker,operation))',
        'claims': 'CREATE TABLE claims (operation TEXT PRIMARY KEY, ticket TEXT)',
        'credentials': 'CREATE TABLE credentials (worker TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE)',
        'closure': 'CREATE TABLE closure (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)'}

    def safe(raw):
        if re.search(rb'sk-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----', raw):
            raise ValueError('credential-shaped bytes refused')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            return
        if isinstance(value, dict):
            for choice in value.get('choices', ()):
                if choice.get('message', {}).get('reasoning_content'):
                    raise ValueError('unexpected private reasoning content; retain original hash, not public payload')

    try:
        if not root.is_dir():
            raise FileNotFoundError('No run root created; no paid result implied')
        for path in sorted(root.rglob('*')):
            if path.is_dir():
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                failures.append({'path': relative, 'reason': 'symlink refused'})
                continue
            if relative == 'broker/permit.txt' or relative.endswith(('.sqlite3-wal', '.sqlite3-shm', '.sqlite-wal', '.sqlite-shm')):
                excluded.append({'path': relative, 'reason': 'ephemeral broker permit or SQLite sidecar; DB copied transactionally'})
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.stat().st_size > 32 * 1024 ** 2:
                    raise ValueError('individual evidence file exceeds 32MiB')
                if relative.endswith('queue.sqlite3') and '/private/gateways/' in '/' + relative:
                    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
                        src.execute('PRAGMA query_only=ON')
                        src.execute('BEGIN')
                        actual = dict(src.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
                        assert actual == schemas and src.execute('PRAGMA user_version').fetchone()[0] == 2
                        tables = {}
                        for name, ddl in schemas.items():
                            dst.execute(ddl)
                            if name == 'credentials':
                                continue  # Never select, hash or copy credential values.
                            rows = src.execute('SELECT * FROM ' + name).fetchall()
                            safe(canonical(rows))
                            if rows:
                                dst.executemany('INSERT INTO ' + name + ' VALUES (' + ','.join('?' for _ in rows[0]) + ')', rows)
                            assert dst.execute('SELECT * FROM ' + name).fetchall() == rows
                            tables[name] = sha(canonical(rows))
                        dst.execute('PRAGMA user_version=2')
                    records.append({'path': relative, 'copy_sha256': sha(target.read_bytes()),
                        'kind': 'credential-free SQLite reconstruction, NOT exact original DB bytes',
                        'original_noncredential_table_hashes_exact': tables, 'credential_values_selected': False})
                elif path.suffix in ('.sqlite3', '.sqlite'):
                    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
                        src.execute('PRAGMA query_only=ON')
                        src.backup(dst)
                    safe(target.read_bytes())
                    records.append({'path': relative, 'copy_sha256': sha(target.read_bytes()),
                        'kind': 'transactional SQLite audit snapshot; all failed/unknown rows retained'})
                else:
                    raw = path.read_bytes()
                    safe(raw)
                    with target.open('xb') as stream:
                        stream.write(raw)
                    records.append({'path': relative, 'original_and_copy_sha256': sha(raw), 'kind': 'exact bytes'})
            except Exception as exc:
                # A refused partial copy must not enter the artifact.
                if target.exists():
                    target.unlink()
                failures.append({'path': relative, 'error_type': type(exc).__name__,
                    'original_sha256': sha(path.read_bytes())})
    except Exception as exc:
        failures.append({'error_type': type(exc).__name__})
    seal(destination / 'AUDIT_COPY_DO_NOT_RESUME.json', {'original_root': str(root),
        'credential_rows_intentionally_not_exported': True, 'no_old_run_mutations': True})
    index = {'schema': 'proteus.coral.directed.audit-copy.v1', 'original_root': str(root),
        'records': records, 'expected_exclusions': excluded, 'failures': failures,
        'incomplete_or_private_payload_refused': bool(failures), 'provider_calls_by_exporter': 0,
        'original_databases_not_modified': True, 'not_a_resumable_copy': True}
    seal(destination / 'audit-copy-index.json', index)
    return index


def run(args):
    if not __debug__:
        raise ValueError('Qualification must run with assertions enabled')
    root = Path(args.out).resolve()
    if root.exists():
        raise ValueError('Qualification root already exists; never retry or overwrite')
    if git('rev-parse', 'HEAD:proteus') != PRODUCTION_TREE or git('diff', 'HEAD', '--', 'proteus'):
        raise ValueError('Production source differs from reviewed tree')
    configuration = plan(args.model, args.mode)
    api_key = None
    if args.mode == 'live':
        if not args.execute_approved_live or args.approval is None:
            raise ValueError('Live mode requires an explicit reviewed approval; fixture is the default')
        approval = load(args.approval)
        if set(approval) != {'approved', 'plan_sha256', 'max_physical_dispatches', 'output_root'}:
            raise ValueError('Live approval has unknown or missing fields')
        if (approval.get('approved') is not True or approval.get('plan_sha256') != sha(canonical(configuration))
                or approval.get('max_physical_dispatches') != MAX_CALLS
                or approval.get('output_root') != str(root)):
            raise ValueError('Live approval does not bind this exact plan')
        if configuration['source_pin']['dirty_patch_hash'] is not None:
            raise ValueError('Approved live mode requires a clean source commit')
        api_key = os.environ.get('DEEPSEEK_API_KEY')
        if not api_key:
            raise ValueError('Approved live mode requires an existing DEEPSEEK_API_KEY environment credential')
        used = Path(args.approval).with_name(Path(args.approval).name + '.used.json')
        with used.open('x') as stream:
            stream.write(canonical({'plan_sha256': sha(canonical(configuration)), 'output_root': str(root)}).decode())
    root.mkdir(parents=True)
    seal(root / 'qualification-source.json', {'sha256': sha(SCRIPT.read_bytes()), 'static_source': SCRIPT.read_text()})
    seal(root / 'plan.json', configuration)
    seal(root / 'source-witness.json', source_witness(configuration['source_pin']))
    claim = {'mode': args.mode, 'plan_sha256': sha(canonical(configuration)), 'original_output_root': str(root)}
    if args.mode == 'live':
        seal(root / 'live-approval.json', approval)
        used_value = load(used)
        seal(root / 'live-approval-used.json', used_value)
        claim.update(approval_sha256=sha(canonical(approval)), used_claim_sha256=sha(canonical(used_value)))
    seal(root / 'exclusive-start-claim.json', claim)
    broker = Broker(root / 'broker', mode=args.mode, model=args.model,
        plan_sha256=sha(canonical(configuration)), api_key=api_key)
    broker.thread.start()
    recipe = lock = registry = None
    try:
        recipe, lock, registry = prepare(root, broker)
        environment = {name: os.environ[name] for name in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if name in os.environ}
        argv = [sys.executable, str(SCRIPT), 'controller', '--out', str(root)]
        with (root / 'first-controller.log').open('xb') as stream:
            first = subprocess.Popen(argv, env=environment, stdout=stream, stderr=subprocess.STDOUT)
            wait_owned(first, 500)
        if first.returncode != 86 or not (root / 'controller-hard-exit.json').is_file():
            raise ValueError('Expected actual controller hard-exit boundary was not reached')
        seal(root / 'single-resume-claim.json', {'same_workspace': str(root / 'run'), 'first_return_code': 86})
        with (root / 'resume-controller.log').open('xb') as stream:
            second = subprocess.Popen([*argv, '--resume'], env=environment, stdout=stream, stderr=subprocess.STDOUT)
            wait_owned(second, 100)
        if second.returncode != 0:
            raise ValueError('Same-workspace resume failed; retain evidence and do not retry')
        seal(root / 'controller-process-observations.json', {
            'first': {'pid': first.pid, 'return_code': first.returncode},
            'resumed': {'pid': second.pid, 'return_code': second.returncode}})
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
    prepare_plan.add_argument('--mode', choices=('fixture', 'live'), default='fixture')
    for command in ('run', 'controller', 'audit', 'export'):
        child = commands.add_parser(command)
        child.add_argument('--out', type=Path, required=True)
        if command == 'run':
            child.add_argument('--mode', choices=('fixture', 'live'), default='fixture')
            child.add_argument('--model', default='deepseek-flash')
            child.add_argument('--approval', type=Path)
            child.add_argument('--execute-approved-live', action='store_true')
        elif command == 'controller':
            child.add_argument('--resume', action='store_true')
        elif command == 'export':
            child.add_argument('--destination', type=Path, required=True)
    child = commands.add_parser('worker')
    child.add_argument('--broker', required=True)
    child.add_argument('--permit', required=True)
    child.add_argument('--model', required=True)
    child.add_argument('--transport-mode', required=True, choices=('fixture', 'live'))
    child.add_argument('--plan-hash', required=True)
    args = parser.parse_args()
    if args.command == 'plan':
        value = plan(args.model, args.mode)
        print(json.dumps({'plan': value, 'plan_sha256': sha(canonical(value))}, indent=2))
    elif args.command == 'worker':
        worker(args)
    elif args.command == 'controller':
        controller(args)
    elif args.command == 'export':
        index = export_audit_copy(args.out, args.destination)
        print(json.dumps({'destination': str(args.destination), 'retained_files': len(index['records']),
            'incomplete': index['incomplete_or_private_payload_refused'], 'provider_calls': 0}))
        if index['incomplete_or_private_payload_refused']:
            raise SystemExit(2)
    elif args.command == 'audit':
        root = args.out.resolve()

        class ReadOnlyBroker:
            def __init__(self):
                self.root = root / 'broker'
                self.mode = load(root / 'exclusive-start-claim.json')['mode']

            def rows(self):
                with sqlite3.connect((self.root / 'dispatches.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
                    db.row_factory = sqlite3.Row
                    db.execute('PRAGMA query_only=ON')
                    return [dict(r) for r in db.execute('SELECT * FROM dispatches ORDER BY slot')]
        print(json.dumps(audit(root, ReadOnlyBroker()), indent=2, allow_nan=False))
    else:
        run(args)


if __name__ == '__main__':
    main()
