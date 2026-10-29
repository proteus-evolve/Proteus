"""True Native and pinned GEPA loops, actual LLM adapter, local HTTP fixture.

These verify mechanism and recovery, not benchmark quality or provider parity.
"""

from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
import threading

import pytest

from proteus.artifacts.codecs import FileContent
from proteus.core.contracts import BudgetLimits, FeedbackVisibility, OperationStatus, PluginRef
from proteus.core.serialization import ContractError, content_hash
from proteus.evidence.records import EvidenceAccess
from proteus.evaluation.tasks import TaskDataset
from proteus.examples.candidate_evaluation import plugin
from proteus.execution.workspace_records import WorkspaceOptions
from proteus.methods.native_evolution import NativeEvolutionOptions
from proteus.recipes.config import ActorSpec, ReferenceSpec, StageSpec
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE
from test_v1_harness_candidates import configured
from test_v1_ledger import run_ledger


@contextmanager
def provider():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            prompt = body['messages'][-1]['content']
            if 'Return exactly one JSON object with numeric score' in prompt:
                value = json.loads(prompt.rsplit('\n', 1)[1])
                ref = value['reference']
                answer = ref['answer'] if isinstance(ref, dict) else ref
                reply = json.dumps({'score': float(value['prediction'] == answer)})
            elif 'Current harness state:' in prompt:
                match = re.search(r'policy.md: factor=(\d+)', prompt.rsplit('Current harness state:', 1)[1])
                factor = int(match.group(1)) if match else None
                if factor is None:
                    reply = '```\nfactor=2\n```'
                elif 'Task input:\n' in prompt:
                    reply = json.dumps([{'tool': 'write_note', 'name': 'prediction', 'text': str(factor * 2)}])
                else:
                    reply = json.dumps([{'tool': 'write_note', 'name': 'policy', 'text': 'factor=2'}]
                                       if 'phase: act.' in prompt else [])
            else:
                reply = '```\nfactor=2\n```'  # GEPA's actual reflection actor.
            data = json.dumps({'choices': [{'message': {'content': reply}}],
                'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def native(root, url, *, episodes=2):
    registry, store, _, recipe = configured(root, url)
    artifact = store.put(PluginRef(name='proteus.file_tree', version='1'),
        {'notes/policy.md': FileContent(b'factor=1')})
    stage = StageSpec(id='episode', role='evolver', operator=plugin('proteus.harness_episode',
        **WorkspaceOptions().to_dict()), failure_policy='record',
        input_types={'seed': 'proteus.artifact_ref@1', 'active': 'proteus.artifact_ref@1',
            'episode': 'proteus.harness_episode@1', 'history': 'proteus.evidence.view@1'},
        output_types={'response': 'proteus.actor_result@1'},
        read_scopes=('proteus.artifact_ref@1', 'proteus.harness_episode@1', 'proteus.evidence.view@1'),
        write_scopes=('proteus.actor_result@1',),
        evidence=EvidenceAccess(evaluation_ids=('score',), splits=('train',)))
    options = NativeEvolutionOptions(episode_stage='episode', evaluations=('score',), episodes=episodes,
        goal='Learn the multiplier using the observed task feedback.', max_turns=20)
    recipe = replace(recipe, method=plugin('proteus.native_evolution', **options.to_dict()),
        actors={**recipe.actors, 'evolver': ActorSpec(model='fixture-model', runtime=plugin('proteus.evolution.llm',
            base_url=url, key_env='PROTEUS_HARNESS_CANDIDATE_KEY'))}, stages=(stage,),
        subject=replace(recipe.subject, initial_artifact=artifact),
        execution=replace(recipe.execution, environment=replace(recipe.execution.environment,
            backend=plugin('proteus.harness_environment'))))
    return registry, store, recipe


def execute(root, registry, store, recipe, **kwargs):
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root / 'run', artifact_source=store.root, **kwargs)
    records = json.loads(sorted((root / 'run').glob('execution-*.json'))[-1].read_text())
    return result, records


def test_native_evolution_updates_the_same_harness_and_uses_shared_evaluation(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTEUS_HARNESS_CANDIDATE_KEY', 'fixture')
    with provider() as (url, calls):
        registry, store, recipe = native(tmp_path, url)
        result, records = execute(tmp_path, registry, store, recipe)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        assert [row['output'] for row in records['rollouts']] == ['2', '4', '4']
        assert [row['rows'][0]['score'] for row in records['evaluations']] == [0, 1, 1]
        assert len(calls) == 11  # Three task trials plus two four-phase episodes.
        assert len(records['actor_calls']) == 2
        assert all(row['role'] == 'evolver' for row in records['actor_calls'])
        assert 'policy.md: factor=2' in calls[6]['messages'][-1]['content']  # Next episode observes changed state.
        usage = records['actor_calls'][1]['result']['runtime']['settings']['native_usage']
        assert usage['version'] == 2 and usage['episode'] == 2 and usage['boundary_count'] == 4
        assert [row['candidate_id'] for row in records['rollouts']] == ['initial', 'episode-1', 'episode-2']
        assert all(row['usage']['model_calls'] == 1 for row in records['rollouts'])
        before = run_ledger(tmp_path / 'run').events()
        resumed, _ = execute(tmp_path, registry, store, recipe, resume=True)
        assert resumed == result and len(calls) == 11
        assert run_ledger(tmp_path / 'run').events() == before


@pytest.mark.parametrize('reflector', ('chat', 'llm', 'process'))
def test_same_llm_integration_is_evolved_by_pinned_gepa(tmp_path, monkeypatch, reflector):
    pytest.importorskip('gepa')
    from proteus.methods.gepa.method import GepaOptions, register
    from proteus.methods.gepa.reference import REPOSITORY, REVISION
    monkeypatch.setenv('PROTEUS_HARNESS_CANDIDATE_KEY', 'fixture')
    with provider() as (url, calls):
        registry, store, _, recipe = configured(tmp_path, url)
        register(registry)
        dataset = TaskDataset.from_json((tmp_path / 'dataset.json').read_text())
        records = tuple(replace(dataset.records[0], task=replace(dataset.records[0].task,
            id=split, split=split)) for split in ('train', 'validation', 'test'))
        dataset = replace(dataset, records=records)
        (tmp_path / 'dataset.json').write_text(dataset.to_json())
        evaluations = tuple(replace(recipe.evaluations[0], id=split,
            tasks=replace(recipe.evaluations[0].tasks, dataset_hash=content_hash(dataset), split=split, task_ids=(split,)),
            purpose='optimization' if split == 'train' else split, selection_eligible=split != 'test',
            visibility=FeedbackVisibility.HIDDEN if split == 'test' else FeedbackVisibility.OBSERVED,
            feedback_timing='never' if split == 'test' else 'after_batch') for split in ('train', 'validation', 'test'))
        options = GepaOptions(train='train', validation='validation', reflection_stage='reflect',
            max_metric_calls=4, minibatch_size=1, test_evaluations=('test',))
        recipe = replace(recipe, method=plugin('proteus.gepa', **options.to_dict()), evaluations=evaluations,
            reference=ReferenceSpec(profile='adapted', code=REPOSITORY, revision=REVISION,
                deviations=('Materialized LLM harness state and local HTTP fixture; no benchmark result.',)),
            actors={**recipe.actors, 'reflection': ActorSpec(model='fixture-reflector', runtime=plugin('proteus.chat',
                base_url=url, key_env='PROTEUS_HARNESS_CANDIDATE_KEY'))},
            stages=(StageSpec(id='reflect', operator=plugin('proteus.prompt'), role='reflection',
                input_types={'prompt': 'proteus.text@1'}, output_types={'response': 'proteus.actor_result@1'},
                read_scopes=('proteus.text@1',), write_scopes=('proteus.actor_result@1',)),),
            search={'parent': plugin('proteus.gepa_parent'), 'frontier': plugin('proteus.gepa_frontier'),
                'final': plugin('proteus.gepa_best')}, budget=BudgetLimits())
        if reflector == 'process':
            from proteus.examples.harness_roles import register as register_process
            bindings = register_process(registry)
            reflection = ActorSpec(runtime=plugin(bindings['actor'].name))
        else:
            reflection = replace(recipe.actors['reflection'], runtime=plugin(
                'proteus.chat' if reflector == 'chat' else 'proteus.harness.llm',
                base_url=url, key_env='PROTEUS_HARNESS_CANDIDATE_KEY'))
        recipe = replace(recipe, actors={**recipe.actors, 'reflection': reflection})
        result, records = execute(tmp_path, registry, store, recipe)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        assert any(row['role'] == 'reflection' for row in records['actor_calls'])
        assert any('policy.md: factor=1' in row['messages'][-1]['content'] for row in calls)
        assert any('policy.md: factor=2' in row['messages'][-1]['content'] for row in calls)
        assert {row['output'] for row in records['rollouts']} == {'2', '4'}
        summary = next(row['value'] for row in records['values'] if row['ref']['kind'] == 'proteus.gepa.summary@1')
        assert len(summary['accepted_ids']) == 2 and summary['reflection_calls'] == 1
        assert {row['candidate_id'] for row in records['rollouts']} == set(summary['accepted_ids'])
        again, _ = execute(tmp_path, registry, store, recipe, resume=True)
        assert again == result


def test_native_role_mismatch_is_preflight_error(tmp_path):
    registry, _, recipe = native(tmp_path, 'http://127.0.0.1:1')
    recipe = replace(recipe, actors={**recipe.actors, 'evolver': replace(recipe.actors['evolver'],
        runtime=plugin('proteus.workspace.pi', image='fixture@sha256:' + 'a' * 64, network='none', phase_timeout_seconds=1))})
    with pytest.raises(ContractError, match='evolution binding'):
        registry.resolve(recipe)


def process_native(root, name='example.process_harness'):
    from proteus.artifacts.store import ArtifactStore
    from proteus.examples.harness_roles import PROGRAM, register
    registry, store, recipe = native(root, 'http://127.0.0.1:1', episodes=3)
    bindings = register(registry, name)
    store = ArtifactStore(store.root, registry)
    artifact = store.put(PluginRef(name='proteus.file_tree', version='1'),
        {'controller.py': FileContent(PROGRAM.encode())})
    recipe = replace(recipe, actors={
        'target': ActorSpec(runtime=plugin(bindings['actor'].name)),
        'evolver': ActorSpec(runtime=plugin(bindings['evolution'].name, break_first=True))},
        subject=replace(recipe.subject, initial_artifact=artifact))
    return registry, store, recipe


def test_invalid_pending_code_is_repaired_by_last_valid_runtime(tmp_path):
    from proteus.core.native_inputs import read_native_inputs
    registry, store, recipe = process_native(tmp_path)
    result, records = execute(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    # The broken proposal was retained, never executed or silently substituted into an evaluation.
    assert [row['candidate_id'] for row in records['rollouts']] == ['initial', 'episode-2', 'episode-3']
    assert [row['output'] for row in records['rollouts']] == ['2', '4', '4']
    first, repair, later = records['actor_calls']
    assert not first['result']['runtime']['settings']['candidate_viability']['valid']
    assert 'SyntaxError' in first['result']['runtime']['settings']['candidate_viability']['diagnostic']
    assert repair['request']['workspace']['seed'] == first['result']['workspace_artifact']
    assert repair['request']['workspace']['active_seed'] == first['request']['workspace']['active_seed']
    assert later['request']['workspace']['active_seed'] == repair['result']['workspace_artifact']
    assert 'Repair the exact pending candidate' in repair['request']['workspace']['episode']['prompts']['act']
    receipts = [read_native_inputs(tmp_path / 'run/actors' / row['id']) for row in records['actor_calls']]
    assert receipts[0][0]['input']['payload']['source_hash'] == receipts[1][0]['input']['payload']['source_hash']
    assert receipts[1][0]['input']['payload']['source_hash'] != receipts[2][0]['input']['payload']['source_hash']
    assert all(row['usage']['model_calls'] == 0 for row in records['actor_calls'])
    assert execute(tmp_path, registry, store, recipe, resume=True)[0] == result


def test_same_integration_is_coding_actor_and_role_is_config_replaceable(tmp_path):
    from proteus.examples.harness_roles import register
    from test_v1_workspace import configured as coding, run as run_coding
    for name in ('example.process_harness', 'example.second_harness'):
        root = tmp_path / name
        registry, recipe = coding(root)
        bindings = register(registry, name)
        recipe = replace(recipe, actors={'proposer': ActorSpec(runtime=plugin(bindings['workspace'].name))})
        result, records = run_coding(root, registry=registry, recipe=recipe)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        assert records['actor_calls'][0]['result']['runtime']['runtime']['name'] == bindings['workspace'].name
        assert len(records['candidates']) == 5
        assert [row['output'] for row in records['rollouts'][-6:]] == [4, 6, 6, 9, 8, 12]
        assert run_coding(root, registry=registry, recipe=recipe, resume=True)[0] == result


NATIVE_WORKER = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_harness_methods import native, execute
from proteus.execution.harnesses import IntegratedHarnessActor
from proteus.core.ledger import Ledger
root,url,boundary,resume=Path(sys.argv[1]),sys.argv[2],sys.argv[3],sys.argv[4]=='resume'
registry,store,recipe=native(root,url,episodes=1)
if not resume:
    if boundary=='uncertain':
        original=IntegratedHarnessActor.execute
        def interrupted(self,*args,**kwargs):
            result=original(self,*args,**kwargs)
            if self.role=='evolution': os._exit(86)
            return result
        IntegratedHarnessActor.execute=interrupted
    else:
        original=Ledger.settle
        def interrupted(self,entry_id,*args,**kwargs):
            result=original(self,entry_id,*args,**kwargs)
            if self.entry(entry_id)['identity']['metadata']['kind']=='actor': os._exit(86)
            return result
        Ledger.settle=interrupted
result,records=execute(root,registry,store,recipe,resume=resume)
(root/'outcome.json').write_text(result.to_json())
'''


@pytest.mark.parametrize('boundary', ('settled', 'uncertain'))
def test_native_hard_exit_never_regenerates_completed_or_unknown_evolution(tmp_path, monkeypatch, boundary):
    import subprocess
    import sys
    monkeypatch.setenv('PROTEUS_HARNESS_CANDIDATE_KEY', 'fixture')
    with provider() as (url, calls):
        command = [sys.executable, '-c', NATIVE_WORKER, str(tmp_path), url, boundary]
        first = subprocess.run([*command, 'start'], capture_output=True, text=True, timeout=30)
        assert first.returncode == 86, first.stdout + first.stderr
        assert len(calls) == 5
        second = subprocess.run([*command, 'resume'], capture_output=True, text=True, timeout=30)
        assert second.returncode == 0, second.stdout + second.stderr
        result = json.loads((tmp_path / 'outcome.json').read_text())
        if boundary == 'settled':
            assert result['status'] == 'completed', result['diagnostic']
            assert len(calls) == 6
        else:
            assert result['status'] == 'failed' and 'RecoveryRequired' in result['diagnostic']
            assert len(calls) == 5


@pytest.mark.parametrize('harness', ('llm', 'process'))
def test_independent_private_model_evaluator_is_shared_across_harnesses(tmp_path, monkeypatch, harness):
    from proteus.recipes.export import report_run
    monkeypatch.setenv('PROTEUS_HARNESS_CANDIDATE_KEY', 'fixture')
    with provider() as (url, calls):
        registry, store, recipe = (native(tmp_path, url, episodes=1) if harness == 'llm' else process_native(tmp_path))
        dataset = TaskDataset.from_json((tmp_path / 'dataset.json').read_text())
        hidden = replace(dataset.records[0], task=replace(dataset.records[0].task, id='hidden', split='test'),
            reference={'answer': '4', 'private': 'PRIVATE-HARNESS-GRADER-REFERENCE'})
        dataset = replace(dataset, records=(*dataset.records, hidden))
        (tmp_path / 'dataset.json').write_text(dataset.to_json())
        score = replace(recipe.evaluations[0], evaluator=plugin('proteus.model_judge'), actor='judge',
            tasks=replace(recipe.evaluations[0].tasks, dataset_hash=content_hash(dataset)))
        test = replace(score, id='hidden', tasks=replace(score.tasks, split='test', task_ids=('hidden',)),
            purpose='test', selection_eligible=False, visibility=FeedbackVisibility.HIDDEN, feedback_timing='never')
        options = replace(NativeEvolutionOptions.from_dict(recipe.method.options), test_evaluations=('hidden',))
        recipe = replace(recipe, method=plugin('proteus.native_evolution', **options.to_dict()),
            evaluations=(score, test), actors={**recipe.actors, 'judge': ActorSpec(model='fixture-judge',
                runtime=plugin('proteus.chat', base_url=url, key_env='PROTEUS_HARNESS_CANDIDATE_KEY'))})
        result, records = execute(tmp_path, registry, store, recipe)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        assert 'PRIVATE-HARNESS-GRADER-REFERENCE' not in json.dumps(records)
        assert all(row['role'] == 'evolver' for row in records['actor_calls'])
        public, owner = report_run(tmp_path / 'run'), report_run(tmp_path / 'run', include_private=True)
        assert next(row['score'] for row in owner['tasks'] if row['visibility'] == 'hidden') == 1
        assert next(row['score'] for row in public['tasks'] if row['visibility'] == 'hidden') is None
        grades = [row for row in run_ledger(tmp_path / 'run').entries()
                  if row['identity']['metadata'].get('kind') == 'grader']
        assert all(row['identity']['metadata']['role'] == 'judge' for row in grades)
        assert all(row['settlement']['result']['grade']['usage']['model_calls'] == 1 for row in grades)
        count = len(calls)
        assert execute(tmp_path, registry, store, recipe, resume=True)[0] == result
        assert len(calls) == count


def test_episode_does_not_inherit_a_single_act_call_bound(tmp_path):
    from proteus.examples.harness_roles import ProcessHarness, ProcessHarnessOptions
    from proteus.execution.harnesses import HarnessIntegration, register_harness
    registry, _, recipe = native(tmp_path, 'http://127.0.0.1:1')
    register_harness(registry, HarnessIntegration(name='fixture.onecall',
        factory=lambda role, spec, options: ProcessHarness('fixture.onecall', role, options),
        options={'actor': ProcessHarnessOptions, 'evolution': ProcessHarnessOptions},
        capabilities=('harness.custom_phases', 'harness.candidate_tree', 'harness.staged_activation'),
        implementation_hash=content_hash('explicit call-bound fault fixture'), source='proteus', model_call_bound=1))
    recipe = replace(recipe, budget=BudgetLimits(max_model_calls=10), actors={
        'target': ActorSpec(model='fixture', runtime=plugin('fixture.onecall.actor')),
        'evolver': ActorSpec(model='fixture', runtime=plugin('fixture.onecall.evolution'))})
    with pytest.raises(ContractError, match='bounded|call bound'):
        registry.resolve(recipe)


@pytest.mark.parametrize('inline,feedback_kinds', ((65536, True), (65536, False), (0, True)))
def test_native_inline_context_obeys_exact_stage_evidence_policy(tmp_path, monkeypatch, inline, feedback_kinds):
    monkeypatch.setenv('PROTEUS_HARNESS_CANDIDATE_KEY', 'fixture')
    with provider() as (url, calls):
        registry, store, recipe = native(tmp_path, url, episodes=1)
        options = replace(NativeEvolutionOptions.from_dict(recipe.method.options), max_inline_evidence_bytes=inline)
        stage = recipe.stages[0]
        if not feedback_kinds:
            stage = replace(stage, evidence=replace(stage.evidence, kinds=('candidate',)))
        recipe = replace(recipe, method=plugin('proteus.native_evolution', **options.to_dict()), stages=(stage,))
        result, records = execute(tmp_path, registry, store, recipe)
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        prompt = records['actor_calls'][0]['request']['workspace']['episode']['prompts']['act']
        assert ('"correct":false' in prompt) == (feedback_kinds and inline > 0)
        if not feedback_kinds:
            assert 'exact_match' not in prompt
        view = next(row['value'] for row in records['values'] if row['ref']['kind'] == 'proteus.evidence.view@1')
        assert ('feedback' in {entry['kind'] for entry in view['entries']}) == feedback_kinds
        if inline == 0:
            assert 'omitted from inline context' in prompt
        assert len(calls) == 6


def test_staged_subject_prediction_uses_the_writable_candidate_path(tmp_path, monkeypatch):
    from proteus.execution.harnesses import IntegratedHarnessActor
    observed = []
    execute_candidate = IntegratedHarnessActor.execute_candidate
    def inspect_request(self, spec, request, workspace):
        observed.append(request.prompt)
        assert 'Write the prediction only to /workspace/candidate/notes/prediction.md.' in request.prompt
        assert 'active /workspace snapshot is read-only' in request.prompt
        return execute_candidate(self, spec, request, workspace)
    monkeypatch.setattr(IntegratedHarnessActor, 'execute_candidate', inspect_request)
    registry, store, recipe = process_native(tmp_path)
    result, _ = execute(tmp_path, registry, store, recipe)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    assert len(observed) == 3


@pytest.mark.parametrize('extra', ({'max_turns': 3}, {'seed': 7}))
def test_evolution_role_rejects_unused_generic_call_controls(tmp_path, extra):
    registry, _, recipe = native(tmp_path, 'http://127.0.0.1:1')
    actor = recipe.actors['evolver']
    actor = replace(actor, runtime=replace(actor.runtime, options={**actor.runtime.options, **extra}))
    with pytest.raises(ContractError, match='unknown fields'):
        registry.resolve(replace(recipe, actors={**recipe.actors, 'evolver': actor}))
