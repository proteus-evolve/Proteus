"""Original geometry scoring, recipe contracts and the actual worker-facing CLI."""

import ast
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

from proteus.artifacts.codecs import FileContent, FileDelta, FileWrite
from proteus.artifacts.store import ArtifactStore
from proteus.benchmarks.coral_circle_packing import ROOT, reference
from proteus.benchmarks.coral_circle_packing.recipe import build_spec, prepare, register
from proteus.benchmarks.coral_circle_packing.verify import BENCHMARK, grade
from proteus.core.contracts import BudgetLimits
from proteus.core.serialization import content_hash
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from proteus.workers.records import CommandWorkerOptions


def grid():
    return {'centers': [[(i % 6+.5)/6, (i//6+.5)/6] for i in range(26)], 'radii': [1/12]*26}


def original_grader():
    # Execute the actual pinned function, including its fresh subprocess. Only
    # the unrelated framework class imports are omitted; its AST is unchanged.
    record = reference()
    source = ROOT/'grader.reference.py'
    external = os.environ.get('PROTEUS_CORAL_SOURCE')
    if external:
        source = Path(external)/record['files'][source.name]['source']
        assert 'sha256:'+hashlib.sha256(source.read_bytes()).hexdigest() == record['files']['grader.reference.py']['sha256']
    parsed = ast.parse(source.read_text())
    nodes = [node for node in parsed.body if isinstance(node, (ast.Import, ast.Assign))
             or (isinstance(node, ast.FunctionDef) and node.name == '_run_evaluation')]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['_run_evaluation']


@pytest.mark.parametrize('case', ['valid', 'overlap', 'outside', 'negative', 'nan', 'shape', 'tolerance', 'zero'])
def test_geometry_matches_actual_reference_grader(tmp_path, case):
    pytest.importorskip('numpy')
    value = grid()
    if case == 'overlap':
        value['centers'][1] = value['centers'][0]
    elif case == 'outside':
        value['centers'][0][0] = -0.5
    elif case == 'negative':
        value['radii'][0] = -1
    elif case == 'nan':
        value['centers'][0][0] = float('nan')
    elif case == 'shape':
        value['centers'].pop()
    elif case == 'tolerance':
        value['centers'][0][0] -= 0.5e-6
    elif case == 'zero':
        value['radii'] = [0]*26
    program = tmp_path/'initial_program.py'
    program.write_text('import json\n'
        f'data=json.loads({json.dumps(value)!r})\n'
        'def run(): return data["centers"],data["radii"],999999\n')
    expected = original_grader()(str(program), 10, [sys.executable])
    result = grade(value)
    assert result['score'] == pytest.approx(expected['score'], abs=1e-14)
    assert result['score'] < 2  # the false claimed sum cannot become reward


def test_unmodified_seed_matches_original_scoring_and_resource_pins(tmp_path):
    pytest.importorskip('numpy')
    original = runpy.run_path(str(ROOT/'seed.reference.py'))
    centers, radii, _ = original['run']()
    value = {'centers': centers.tolist(), 'radii': radii.tolist()}
    source = tmp_path/'initial_program.py'
    source.write_bytes((ROOT/'seed.reference.py').read_bytes())
    expected = original_grader()(str(source), 10, [sys.executable])
    assert grade(value)['score'] == pytest.approx(expected['score'], abs=1e-14)
    assert expected['score'] > 0


@pytest.mark.parametrize('value', [None, {}, {'centers': ['12']*26, 'radii': [0]*26},
    {'centers': [[.5, .5]]*26, 'radii': '0'*26}, {'centers': [[math.inf, .5]]*26, 'radii': [0]*26}])
def test_private_verifier_rejects_malformed_or_nonfinite_geometry(value):
    assert grade(value)['score'] == 0


def test_claimed_score_and_sum_are_never_authoritative():
    value = grid()
    value.update(score=99999, sum_radii=99999)
    assert grade(value)['score'] == pytest.approx(sum(value['radii'])/BENCHMARK)


@pytest.mark.parametrize('workers', [0, 1, 2])
def test_prepared_studies_share_original_task_and_equal_formal_budget(tmp_path, workers):
    options = CommandWorkerOptions(command=(sys.executable,), command_files={sys.executable:
        'sha256:'+hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()})
    path = prepare(tmp_path/'study', '/pinned/harbor/python', workers=max(1, workers), submissions=7,
        worker_runtime=plugin('proteus.worker.command', **options.to_dict()) if workers else None,
        runtime={'runtime_hash': content_hash('fixture')})
    recipe = load_recipe(path)
    registry = builtin_registry()
    register(registry)
    assert registry.resolve(recipe) == recipe
    source = ArtifactStore(path.parent/'artifact-store', registry).files(recipe.subject.initial_artifact)
    assert source['initial_program.py'].data == (ROOT/'seed.reference.py').read_bytes()
    assert recipe.execution.environment.build_hash == content_hash(build_spec())
    assert recipe.evaluations[-1].purpose == 'test' and not recipe.evaluations[-1].selection_eligible
    assert recipe.budget.max_evaluations == (9 if workers else 2)
    assert len(recipe.workers) == workers
    if workers:
        assert recipe.gateways[0].evaluations == ('packing',)
        assert recipe.method.options['final_evaluation'] == 'confirmation'
        assert recipe.event_policies[0].operator.options['profile'] == 'reference_manager'


def test_research_cli_uses_formal_gateway_and_sourced_knowledge(tmp_path):
    from test_v1_worker_knowledge import setup
    from test_v1_gateway import services
    recipe, registry, lock = setup(tmp_path)
    current = services(tmp_path, recipe, registry, lock)
    gateway = current.gateway('formal')
    candidate = tmp_path/'candidate'
    candidate.mkdir()
    code = 'import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({"output":r["task"]["input"]*2,"state":r["state"]}))\n'
    (candidate/'main.py').write_text(code)
    note = tmp_path/'note.md'
    note.write_text('Double each input.\n')

    def command(*args):
        environment = {**os.environ, **gateway.connection('a')}
        completed = subprocess.run([sys.executable, str(ROOT/'research.py'), *args], env=environment,
                                   capture_output=True, text=True, check=True, timeout=10)
        return json.loads(completed.stdout)

    with gateway.serve():
        published = command('publish', '--operation', 'idea-1', '--kind', 'notes', '--key', 'double', '--file', str(note))
        snapshot = command('snapshot', '--operation', 'view-1')
        revision = published['record']['revision']
        ref = snapshot['snapshot']['revisions'][0]
        assert command('read', ref['content_hash'])['revision'] == revision
        ticket = command('submit', '--operation', 'candidate-1', '--evaluation', 'validation', '--directory', str(candidate))
        assert command('submit', '--operation', 'candidate-1', '--evaluation', 'validation', '--directory', str(candidate)) == ticket
        (candidate/'main.py').write_text('raise RuntimeError("not submitted")')
        result = gateway.step('formal-1', timeout_seconds=5)
        response = command('poll', ticket['ticket_id'])
        assert response['status'] == result.status == 'completed'
        assert current.artifacts.files(result.candidate.artifact)['main.py'] == FileContent(code.encode())
        sources = tmp_path/'sources.json'
        sources.write_text(json.dumps([ref, result.shared_attempt.to_dict()]))
        skill = command('publish', '--operation', 'skill-1', '--kind', 'skills', '--key', 'double', '--file', str(note),
                        '--sources', str(sources))
        assert skill['record']['revision']['write']['sources'] == json.loads(sources.read_text())
    assert current.ledger.balance()['active_slots'] == 0


def test_research_tree_excludes_dependencies_and_rejects_symlinks(tmp_path):
    from proteus.benchmarks.coral_circle_packing.research import tree
    (tmp_path/'main.py').write_text('print(1)')
    (tmp_path/'.proteus-deps').mkdir()
    (tmp_path/'.proteus-deps/large-binary').write_bytes(b'not candidate source')
    assert [row['path'] for row in tree(tmp_path)] == ['main.py']
    (tmp_path/'escape').symlink_to(tmp_path/'main.py')
    with pytest.raises(ValueError, match='symbolic'):
        tree(tmp_path)


@pytest.mark.skipif(not os.environ.get('PROTEUS_HARBOR_PYTHON'), reason='requires pinned Harbor and real Docker')
def test_live_harbor_original_packing_seed_improvement_forgery_and_resume(tmp_path):
    root = Path(os.environ.get('PROTEUS_HARBOR_STUDY', str(tmp_path)))/'coral-circle-packing'
    path = prepare(root/'study', os.environ['PROTEUS_HARBOR_PYTHON'])
    registry = builtin_registry()
    register(registry)
    recipe = load_recipe(path)
    valid = grid()
    invalid = {**valid, 'centers': [[.5, .5]]*26}
    variants = []
    for name, value in [('grid', valid), ('forged', invalid)]:
        program = f'def run(): return {value["centers"]!r}, {value["radii"]!r}, 999999\n'
        delta = FileDelta(base_hash=recipe.subject.initial_artifact.content_hash,
                         put=(FileWrite.from_bytes('initial_program.py', program.encode()),))
        variants.append({'id': name, 'delta': delta.to_dict()})
    recipe = registry.resolve(replace(recipe, method=plugin('proteus.compare',
        evaluations=['packing', 'confirmation'], variants=variants), budget=BudgetLimits(max_evaluations=6)))
    from test_v1_evaluation import SOURCE
    lock = registry.make_lock(recipe, [SOURCE])
    result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run', artifact_source=path.parent/'artifact-store')
    assert result.status.value == 'completed', result.diagnostic
    execution = json.loads((root/'run/execution-0001.json').read_text())
    observed = [row['rows'][0]['score'] for row in execution['evaluations']]
    assert observed == pytest.approx([0.36410189352798084, grade(valid)['score'], 0], abs=1e-12)
    from test_v1_ledger import run_ledger
    ledger = run_ledger(root/'run')
    assert ledger.balance()['charged']['evaluations'] == 6 and ledger.balance()['active_slots'] == 0
    before = ledger.events()
    assert run_recipe(recipe, registry=registry, lock=lock, root=root/'run', resume=True) == result
    assert ledger.events() == before
    (root/'verified.json').write_text(json.dumps({'observed_scores': observed, 'evaluations': 6,
                                               'no_repeat_resume': True}))
