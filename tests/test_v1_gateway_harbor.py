"""Real autonomous submissions use Harbor's candidate loader and private verifier."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from proteus.artifacts.codecs import FileWrite
from proteus.core.contracts import BudgetLimits, FeedbackVisibility
from proteus.core.methods import RunServices
from proteus.core.serialization import content_hash
from proteus.examples import harbor_evaluation
from proteus.examples.evaluation_gateway import prepare, register
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from proteus.workers.records import CommandWorkerOptions
from test_v1_evaluation import SOURCE
from test_v1_ledger import run_ledger


# A pinned, model-free research worker. Its only formal-evaluation interface is
# the authenticated gateway; neither the answer verifier nor a score is supplied.
WORKER = '''import json, os, sys, time, urllib.request
from pathlib import Path
launch = json.loads(sys.stdin.readline())
worker = launch['identity']['worker_id']
body = json.loads(Path(sys.argv[1]).read_text())[worker]
def request(path, body=None):
    request = urllib.request.Request(os.environ['PROTEUS_EVALUATION_URL'] + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+os.environ['PROTEUS_EVALUATION_TOKEN'],
                 'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)
ticket = request('/v1/submissions', body)
Path('ticket.json').write_text(json.dumps(ticket))
Path('main.py').write_text("raise RuntimeError('post-submission edit')\\n")
deadline = time.monotonic()+240
while time.monotonic()<deadline:
    result = request('/v1/submissions/'+ticket['ticket_id'])
    if result['status'] in ('completed','failed'):
        Path('response.json').write_text(json.dumps(result))
        assert result['status']=='completed'
        print(json.dumps({'event':'result','session_id':'harbor-'+worker,'resumed_from':None,
            'usage':{'model_calls':0,'input_tokens':0,'output_tokens':0,'cost_usd':0}}),flush=True)
        break
    time.sleep(.05)
else:
    raise TimeoutError('formal Harbor evaluation did not finish')
'''


@pytest.mark.skipif(not os.environ.get('PROTEUS_HARBOR_PYTHON'), reason='requires pinned Harbor runtime and live Docker')
def test_live_harbor_gateway_autonomous_workers_frozen_candidates_and_private_verifiers(tmp_path):
    root = Path(os.environ.get('PROTEUS_HARBOR_STUDY', str(tmp_path))) / 'evaluation-gateway'
    recipe = load_recipe(harbor_evaluation.prepare(root/'study', os.environ['PROTEUS_HARBOR_PYTHON']))
    template = load_recipe(prepare(root/'worker-template'))
    script = root/'worker.py'
    script.write_text(WORKER)
    data = root/'submissions.json'
    submissions = {}
    for worker, answer in (('a', 41), ('b', 42)):
        submissions[worker] = {'operation_id': 'proposal', 'evaluation_id': 'reward', 'files': [
            FileWrite.from_bytes('main.py', harbor_evaluation.SOURCE.encode()).to_dict(),
            FileWrite.from_bytes('value.py', f'ANSWER = {answer}\n'.encode()).to_dict()]}
    data.write_text(json.dumps(submissions))
    command = (sys.executable, str(script))
    options = CommandWorkerOptions(command=(*command, str(data)),
        command_files={name: 'sha256:'+hashlib.sha256(Path(name).read_bytes()).hexdigest()
                       for name in (*command, str(data))}, timeout_seconds=270, shutdown_grace_seconds=.2)
    stage = replace(template.stages[0], evidence=replace(template.stages[0].evidence, evaluation_ids=('reward',)))
    public = recipe.evaluations[0]
    private = replace(public, id='test', purpose='test', visibility=FeedbackVisibility.HIDDEN,
                      feedback_timing='never', selection_eligible=False)
    recipe = replace(recipe, method=template.method, actors=template.actors, stages=(stage,),
        workers=tuple(replace(worker, runtime=plugin('proteus.worker.command', **options.to_dict()))
                      for worker in template.workers),
        gateways=(replace(template.gateways[0], evaluations=('reward',), max_evaluations_per_worker=1),),
        knowledge=(replace(template.knowledge[0], evidence=stage.evidence),),
        evaluations=(public, private), budget=BudgetLimits(max_evaluations=3, max_concurrency=3))
    registry = builtin_registry()
    register(registry)
    recipe = registry.resolve(recipe)
    lock = registry.make_lock(recipe, [SOURCE])
    (root/'recipe.json').write_text(recipe.to_json())
    (root/'lock.json').write_text(lock.to_json())
    result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run',
                        artifact_source=root/'study/artifact-store')
    assert result.status.value == 'completed', result.diagnostic
    records = json.loads((root/'run/execution-0001.json').read_text())
    assert len(records['rollouts']) == 3
    assert sorted(row['score'] for batch in records['evaluations'] for row in batch['rows']) == [0, 1]
    current = RunServices(run_id='run', recipe=recipe, registry=registry, root=root/'run', lock_hash=content_hash(lock))
    for worker, answer in (('a', 41), ('b', 42)):
        controller = current.worker(worker).controller
        response = json.loads((controller.workspace/'response.json').read_text())
        disclosed = b'\n'.join(FileWrite.from_dict(item).content().data for item in response['files'])
        assert b'PROTEUS_PRIVATE_VERIFIER_23' not in disclosed and b'post-submission edit' not in disclosed
        assert response['result']['shared_attempt'] is not None
        assert {entry['evaluation_id'] for entry in response['view']['entries']} == {None, 'reward'}
        ticket = response['ticket']
        code = current.artifacts.files(current.gateway('formal').store.poll(worker,
            json.loads((controller.workspace/'ticket.json').read_text())['ticket_id']).ticket.artifact)
        assert code['value.py'].data == f'ANSWER = {answer}\n'.encode()
        assert ticket['worker_id'] == worker
    assert len(current.knowledge('research').store.snapshot().revisions) == 2
    proofs = []
    for path in (root/'run/evaluations').glob('*/trials/*/private/response.json'):
        response = json.loads(path.read_text())
        agent, verifier = response['loaded_proof'], response['verifier_proof']
        assert agent['container_id'] != verifier['container_id']
        assert json.loads(path.with_name('cleanup.json').read_text())['verified']
        proofs.append(agent)
    assert len(proofs) == 3 and len({proof['container_id'] for proof in proofs}) == 3
    book = run_ledger(root/'run')
    assert book.balance()['charged']['evaluations'] == 3 and book.balance()['active_slots'] == 0
    before = book.events()
    assert run_recipe(recipe, registry=registry, lock=lock, root=root/'run', resume=True) == result
    assert book.events() == before
