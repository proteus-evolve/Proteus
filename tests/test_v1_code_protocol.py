"""Managed candidate prediction/learning through real child processes and the ledger."""

import json
from dataclasses import replace

import pytest

from proteus.artifacts.codecs import FileContent, FileDelta, FileWrite
from proteus.core.contracts import BudgetLimits, OperationStatus, Outcome, PluginRef, RuntimeIdentity, StatePolicy, Usage
from proteus.core.methods import ActorResult, MethodResult, RunServices
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import ContractError, content_hash
from proteus.execution.code_records import CodeCall, CodeOptions, CodeState
from proteus.execution.code_rpc import invoke
from proteus.recipes.config import ActorSpec
from test_v1_evaluation import method, plugin, setup


SOURCE = '''import json, os
class Memory:
    def __init__(self, llm):
        self.llm, self.memory = llm, {}
    def learn_from_batch(self, rows):
        for row in rows:
            self.memory[str(row['input'])] = self.llm(row['ground_truth'])
    def predict(self, text):
        print('ordinary stdout is not a model request')
        return self.llm(self.memory.get(str(text), 'unknown')), {'count': len(self.memory), 'env': sorted(os.environ)}
    def get_state(self):
        return json.dumps(self.memory, sort_keys=True)
    def set_state(self, state):
        self.memory = json.loads(state)
'''


class CountingActor:
    model_call_bound = 1

    def execute(self, spec, request, workspace):
        assert request.timeout_seconds is not None and request.timeout_seconds > 0
        return ActorResult(text=request.prompt, runtime=RuntimeIdentity(runtime=spec.runtime.plugin),
            usage=Usage(model_calls=1, input_tokens=3, output_tokens=2, cost_usd=.01))


def fixture(tmp_path, *, source=SOURCE, max_calls=20, sequence=False, options=None):
    registry, store, dataset, recipe = setup(tmp_path, sequence=sequence, references=('a', 'b'))
    artifact = store.put(PluginRef(name='proteus.file_tree', version='1'), {'candidate.py': FileContent(source.encode())})
    registry.register(Plugin(ref=PluginRef(name='fixture.count', version='1'), kind='actor', options=EmptyOptions,
        factory=lambda options: CountingActor(), implementation_hash=content_hash('fixture.count'), source='proteus',
        capabilities=('actor.execute', 'calls.bounded', 'request.timeout', 'session.fresh')))
    recipe = replace(recipe, subject=replace(recipe.subject, initial_artifact=artifact,
        runner=plugin('proteus.code_subject', **(options or {})), actor='model', input_types={}),
        actors={'model': ActorSpec(runtime=plugin('fixture.count'))}, budget=BudgetLimits(max_model_calls=max_calls))
    recipe = method(registry, recipe, lambda services: MethodResult())
    recipe = registry.resolve(recipe)
    services = RunServices(run_id='run', recipe=recipe, registry=registry, root=tmp_path/'run')
    services.artifacts.copy_from(store, artifact)
    return services


def memory_ref(services, state):
    return services.values.publish('proteus.code.state@1', state)


def learn(services, candidate, *, operation_id=None):
    return services.invoke_candidate(candidate, CodeCall(method='learn_from_batch',
        state=CodeState(artifact_hash=candidate.artifact.content_hash),
        args=([{'input': 0, 'ground_truth': 'a'}, {'input': 1, 'ground_truth': 'b'}],)), operation_id=operation_id)


def test_learning_and_prediction_use_managed_calls_and_separate_state(tmp_path, monkeypatch):
    monkeypatch.setenv('MUST_NOT_INHERIT_API_KEY', 'secret')
    services = fixture(tmp_path)
    candidate = services.initial_candidate()
    learned = learn(services, candidate)
    assert learned.status == Outcome.SUCCEEDED, learned.diagnostic
    assert json.loads(learned.state.value) == {'0': 'a', '1': 'b'}
    assert learned.usage.model_calls == 2 and services.rollouts == []
    ref = memory_ref(services, learned.state)
    receipt = services.evaluate(candidate, 'score', inputs={'memory': ref})
    assert receipt.batches[0].mean_score() == 1
    assert len(services.actor_calls) == 4
    assert services.ledger.balance()['charged']['model_calls'] == 4
    assert services.ledger.balance()['charged']['evaluations'] == 2
    assert all(entry['state'] == 'completed' for entry in services.ledger.entries())
    for rollout in services.rollouts:
        trace = services.values.get(rollout.trace[0])
        assert 'MUST_NOT_INHERIT_API_KEY' not in trace.to_json()
        assert 'ordinary stdout' in trace.to_json()
        assert rollout.usage.model_calls == 1
    assert services.artifacts.get(candidate.artifact)['candidate.py'].data == SOURCE.encode()
    assert services.values.get(ref) == learned.state


@pytest.mark.parametrize('sequence,expected', [(False, [1, 1]), (True, [1, 2])])
def test_prediction_state_reset_and_explicit_sequence_carry(tmp_path, sequence, expected):
    source = SOURCE.replace("return self.llm(self.memory.get(str(text), 'unknown')),", "self.memory['n'] = self.memory.get('n', 0) + 1\n        return self.memory['n'],")
    services = fixture(tmp_path, source=source, sequence=sequence)
    candidate = services.initial_candidate()
    ref = memory_ref(services, CodeState(artifact_hash=candidate.artifact.content_hash))
    services.evaluate(candidate, 'score', inputs={'memory': ref})
    assert [item.output for item in services.rollouts] == expected
    assert services.ledger.balance()['charged']['model_calls'] == 0


def test_state_cannot_cross_candidate_code_versions_and_operations_are_declared(tmp_path):
    services = fixture(tmp_path)
    first = services.initial_candidate()
    learned = learn(services, first)
    second = services.fork_candidate(first, delta=FileDelta(base_hash=first.artifact.content_hash,
        put=(FileWrite.from_bytes('candidate.py', (SOURCE+'\n# another version\n').encode()),)))
    for candidate, call in ((second, CodeCall(method='learn_from_batch', state=learned.state)),
                            (first, CodeCall(method='erase_all', state=learned.state))):
        with pytest.raises(ContractError):
            services.invoke_candidate(candidate, call)
    assert len(services.actor_calls) == 2
    services.evaluate(second, 'score', inputs={'memory': memory_ref(services, learned.state)})
    assert all(row.status == Outcome.INFRA_ERROR for row in services.evaluations[0].rows)
    assert len(services.actor_calls) == 2


def test_finite_call_budget_stops_candidate_requests_before_overdispatch(tmp_path):
    services = fixture(tmp_path, max_calls=1)
    candidate = services.initial_candidate()
    result = learn(services, candidate)
    assert result.status == Outcome.INFRA_ERROR and 'budget exhausted' in result.diagnostic
    assert not result.state.initialized
    assert result.usage.model_calls == 1
    assert len(services.actor_calls) == services.ledger.balance()['charged']['model_calls'] == 1
    assert services.ledger.balance()['active_slots'] == 0


@pytest.mark.parametrize('change,expected', [
    ("raise ValueError('broken candidate')", Outcome.INVALID_CANDIDATE),
    ("while True: pass", Outcome.TIMED_OUT),
    ("print('x' * 100000)", Outcome.INVALID_CANDIDATE),
])
def test_candidate_errors_timeouts_and_output_limits_keep_old_state(tmp_path, change, expected):
    source = SOURCE.replace("print('ordinary stdout is not a model request')", change)
    services = fixture(tmp_path, source=source, options={'max_output_bytes': 1024})
    services.recipe = replace(services.recipe, execution=replace(services.recipe.execution, timeout_seconds=.3))
    candidate = services.initial_candidate()
    state = CodeState(artifact_hash=candidate.artifact.content_hash)
    result = services.invoke_candidate(candidate, CodeCall(method='predict', state=state, args=('x',)))
    assert result.status == expected, result.diagnostic
    assert result.state == state
    assert services.ledger.balance()['active_slots'] == 0
    assert len(result.trace.to_json()) < 10000


def test_model_failure_is_not_returned_as_candidate_text(tmp_path, monkeypatch):
    services = fixture(tmp_path)
    def fail(self, spec, request, workspace):
        raise OSError('transport failed')
    monkeypatch.setattr(CountingActor, 'execute', fail)
    candidate = services.initial_candidate()
    result = learn(services, candidate)
    assert result.status == Outcome.INFRA_ERROR and 'transport failed' in result.diagnostic
    assert result.output is None and not result.state.initialized
    assert result.usage.model_calls is None and services.ledger.balance()['active_slots'] == 0


def test_missing_interface_or_invalid_serialized_state_rejects_candidate(tmp_path):
    services = fixture(tmp_path, source=SOURCE.replace('def get_state(self):', 'def another_name(self):'))
    candidate = services.initial_candidate()
    result = learn(services, candidate)
    assert result.status == Outcome.INVALID_CANDIDATE and 'interface' in result.diagnostic
    assert result.usage.model_calls == 0


def test_protocol_rejects_undeclared_actor_timeout_and_worker_execution(tmp_path):
    services = fixture(tmp_path)
    registry, recipe = services.registry, services.recipe
    plugin_record = registry.get(recipe.actors['model'].runtime.plugin)
    registry._plugins[(plugin_record.ref.name, plugin_record.ref.version)] = replace(plugin_record,
        capabilities=tuple(cap for cap in plugin_record.capabilities if cap != 'request.timeout'))
    with pytest.raises(ContractError, match='transport timeouts'):
        registry.resolve(recipe)
    with pytest.raises(ContractError):
        replace(recipe.execution, state_policy=StatePolicy.WORKER)


def test_direct_rpc_binary_stdout_and_large_model_reply_are_framed(tmp_path):
    (tmp_path/'candidate.py').write_text(SOURCE)
    text = 'a' * 100000
    def actor(prompt, *, timeout_seconds):
        return ActorResult(text=text, runtime=RuntimeIdentity(runtime=PluginRef(name='fixture.actor', version='1')),
                           usage=Usage(model_calls=1))
    call = CodeCall(method='predict', state=CodeState(artifact_hash=content_hash('code')), args=('x',))
    reply = invoke(tmp_path, CodeOptions(), call, actor, timeout_seconds=3)
    assert reply.status == Outcome.SUCCEEDED, reply.diagnostic
    assert reply.output[0] == text


@pytest.mark.parametrize('baseline', ['no_memory', 'fewshot_all'])
def test_pinned_meta_harness_memory_class_state_and_prompt_conformance(tmp_path, baseline):
    import os
    from check_meta_harness_code_reference import check
    source = os.environ.get('PROTEUS_META_HARNESS_SOURCE')
    if not source:
        pytest.skip('pinned Meta-Harness source was not provided')
    check(source, tmp_path, baseline)


RECOVERY_WORKER = r'''
import os, sys
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, str(Path.cwd()/'tests'))
from test_v1_code_protocol import fixture, learn, memory_ref, CountingActor
from test_v1_evaluation import SOURCE as SOURCE_PIN
from proteus.core.contracts import PluginRef
from proteus.core.methods import MethodResult
from proteus.core.plugins import Plugin, EmptyOptions
from proteus.core.serialization import content_hash
from proteus.core.recovery import Journal
from proteus.core.ledger import Ledger
from proteus.recipes.runner import run_recipe
from proteus.examples.candidate_evaluation import plugin
root, boundary, resume = Path(sys.argv[1]), sys.argv[2], sys.argv[3]=='resume'
root.mkdir(exist_ok=True, parents=True)
marker = root/'calls'
original = CountingActor.execute
CountingActor.execute = lambda self, spec, request, workspace: counted(self,spec,request,workspace)
def counted(self,spec,request,workspace):
    with marker.open('a') as out: out.write('call\n')
    return original(self,spec,request,workspace)
services = fixture(root)
class Study:
    def run(self, services, restored_state):
        candidate = services.initial_candidate()
        learned = learn(services,candidate,operation_id='learn')
        assert learned.status.value=='succeeded', learned.diagnostic
        ref = memory_ref(services,learned.state)
        services.evaluate(candidate,'score',inputs={'memory':ref})
        return MethodResult(outputs={'memory':ref})
registry = services.registry
registry.register(Plugin(ref=PluginRef(name='fixture.code_study',version='1'),kind='method',options=EmptyOptions,
    factory=lambda options:Study(),implementation_hash=content_hash('fixture.code_study'),source='proteus',
    capabilities=('run.journal','run.resume'),state_schema=EmptyOptions))
recipe = registry.resolve(replace(services.recipe,method=plugin('fixture.code_study')))
if not resume:
    settle = Ledger.settle
    def settled(self, entry_id, **kwargs):
        value = settle(self,entry_id,**kwargs)
        kind = self.entry(entry_id)['identity']['metadata']['kind']
        if boundary==kind+'.settled': os._exit(86)
        return value
    Ledger.settle = settled
    call = Journal.call
    def recorded(self, kind, *args, **kwargs):
        result = call(self,kind,*args,**kwargs)
        if boundary==kind: os._exit(86)
        return result
    Journal.call = recorded
lock = registry.make_lock(recipe,[SOURCE_PIN])
result = run_recipe(recipe,registry=registry,lock=lock,root=root/'actual-run',resume=resume,
                    artifact_source=services.artifacts.root)
(root/'result.json').write_text(result.to_json())
'''


@pytest.mark.parametrize('boundary,expected_calls', [('code_invocation.settled',4), ('code.call',4), ('actor.settled',1)])
def test_code_call_recovery_reuses_settled_results_and_stops_uncertain_execution(tmp_path, boundary, expected_calls):
    import subprocess
    import sys
    from proteus.core.methods import MethodResult
    command = [sys.executable,'-c',RECOVERY_WORKER,str(tmp_path),boundary]
    first = subprocess.run([*command,'start'],capture_output=True,text=True,timeout=30)
    assert first.returncode==86, first.stderr
    resumed = subprocess.run([*command,'resume'],capture_output=True,text=True,timeout=30)
    assert resumed.returncode==0, resumed.stdout+resumed.stderr
    result=MethodResult.from_json((tmp_path/'result.json').read_text())
    assert len((tmp_path/'calls').read_text().splitlines())==expected_calls
    if boundary=='actor.settled':
        assert result.status==OperationStatus.FAILED and 'RecoveryRequired' in result.diagnostic
    else:
        assert result.status==OperationStatus.COMPLETED, result.diagnostic
        records=json.loads((tmp_path/'actual-run/execution-0001.json').read_text())
        assert len(records['actor_calls'])==4 and len(records['rollouts'])==2
        assert records['evaluations'][0]['rows'][0]['score']==1
    second = subprocess.run([*command,'resume'],capture_output=True,text=True,timeout=30)
    assert second.returncode==0, second.stderr
    assert len((tmp_path/'calls').read_text().splitlines())==expected_calls


@pytest.mark.parametrize('options,diagnostic', [({'max_model_requests':0},'model request limit'),
                                              ({'max_trace_bytes':1024},'trace byte limit')])
def test_rpc_request_and_trace_limits_stop_before_unbounded_growth(tmp_path, options, diagnostic):
    services=fixture(tmp_path, options=options)
    candidate=services.initial_candidate()
    result=services.invoke_candidate(candidate,CodeCall(method='predict',state=CodeState(artifact_hash=candidate.artifact.content_hash),
        args=('x'*2048,)))
    # The fixture fallback prompt is small; make the trace limit case receive a long prompt via learned memory.
    if 'max_trace_bytes' in options:
        memory=CodeState(artifact_hash=candidate.artifact.content_hash,initialized=True,value=json.dumps({'x':'y'*3000}))
        result=services.invoke_candidate(candidate,CodeCall(method='predict',state=memory,args=('x',)))
    assert result.status==Outcome.INVALID_CANDIDATE and diagnostic in result.diagnostic
    assert services.ledger.balance()['active_slots']==0


def test_installed_code_memory_example_protocol():
    from check_code_memory_package import check
    check()


def test_parent_hard_exit_does_not_leave_candidate_process_running(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time
    marker = tmp_path/'pid'
    source = SOURCE.replace("print('ordinary stdout is not a model request')",
        f"__import__('pathlib').Path({str(marker)!r}).write_text(str(os.getpid()))\n        while True: pass")
    (tmp_path/'candidate.py').write_text(source)
    script = r'''
import os, sys, threading, time
from pathlib import Path
from proteus.execution.code_rpc import invoke
from proteus.execution.code_records import CodeCall, CodeOptions, CodeState
from proteus.core.serialization import content_hash
root=Path(sys.argv[1])
def exit_after_child_starts():
    deadline=time.monotonic()+5
    while not (root/'pid').exists() and time.monotonic()<deadline: time.sleep(.01)
    os._exit(86)
threading.Thread(target=exit_after_child_starts,daemon=True).start()
invoke(root,CodeOptions(),CodeCall(method='predict',state=CodeState(artifact_hash=content_hash('code')),args=('x',)),
       lambda *args,**kwargs:None,timeout_seconds=30)
'''
    parent = subprocess.run([sys.executable,'-c',script,str(tmp_path)],capture_output=True,text=True,timeout=10)
    assert parent.returncode==86, parent.stderr
    pid = int(marker.read_text())
    try:
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            status = subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True).stdout.strip()
            if not status or status.startswith('Z'):
                break
            time.sleep(.05)
        else:
            pytest.fail('orphan candidate still runs after its host exited')
    finally:
        try:
            os.killpg(pid,signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_remaining_timeout_reaches_actual_chat_transport(tmp_path, monkeypatch):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            time.sleep(.8)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"a"}}]}')
            except (BrokenPipeError, ConnectionResetError):
                pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    monkeypatch.setenv('CODE_PROTOCOL_FIXTURE_KEY','fixture-only')
    try:
        services=fixture(tmp_path)
        recipe=replace(services.recipe,
            actors={'model':ActorSpec(runtime=plugin('proteus.chat',base_url=f'http://127.0.0.1:{server.server_port}/v1',
                key_env='CODE_PROTOCOL_FIXTURE_KEY',timeout_seconds=30),model='fixture')},
            execution=replace(services.recipe.execution,timeout_seconds=.25))
        services.recipe=services.registry.resolve(recipe)
        candidate=services.initial_candidate()
        started=time.monotonic()
        result=learn(services,candidate)
        elapsed=time.monotonic()-started
        assert result.status in (Outcome.INFRA_ERROR, Outcome.TIMED_OUT), result.diagnostic
        assert elapsed < 2 and len(calls)==1
        assert result.usage.model_calls is None
        assert 0 < services.actor_calls[0]['request']['timeout_seconds'] <= .25
        assert services.ledger.balance()['active_slots']==0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_missing_candidate_module_cannot_fall_back_to_an_installed_module(tmp_path):
    services=fixture(tmp_path,options={'module':'json'})
    candidate=services.initial_candidate()
    result=learn(services,candidate)
    assert result.status==Outcome.INVALID_CANDIDATE and 'not present in the loaded candidate' in result.diagnostic
    assert result.usage.model_calls==0
