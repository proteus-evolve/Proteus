"""Evidence disclosure through real candidate evaluation, stages and process recovery."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from proteus.artifacts.codecs import FileDelta, FileWrite
from proteus.core.contracts import FeedbackVisibility, OperationStatus, PluginRef, ValidationStatus
from proteus.core.methods import MethodResult, RunServices, StageContext
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import ContractError, canonical_json, content_hash
from proteus.evidence.records import EvidenceAccess, EvidenceView
from proteus.evidence.service import EvidenceService
from proteus.evidence.store import EvidenceStore
from proteus.recipes.builtins import Text
from proteus.recipes.config import ActorSpec, StageSpec
from test_v1_evaluation import plugin, setup


class Reader:
    def run(self, context, inputs):
        view = inputs['history']
        files = {entry.path: context.read_evidence(view, entry.path).decode('utf-8')
                 for entry in view.entries if entry.kind == 'artifact'}
        assert context.search_evidence(view, 'json')
        return {'response': Text(text=json.dumps(files))}


class HistoryMethod:
    def run(self, services, restored_state):
        first = services.initial_candidate()
        services.evaluate(first, 'score')
        before = services.evidence_view('inspect')
        services.stage('inspect', {'history': before})
        second = services.fork_candidate(first, candidate_id='second')
        services.evaluate(second, 'score')
        after = services.evidence_view('inspect')
        services.stage('inspect', {'history': after})
        return MethodResult(outputs={'before': before, 'after': after})


def fixture(tmp_path, *, access=None, copy_artifacts=True, **kwargs):
    registry, store, dataset, recipe = setup(tmp_path, **kwargs)
    registry.register(Plugin(ref=PluginRef(name='fixture.history', version='1'), kind='method', options=EmptyOptions,
        factory=lambda options: HistoryMethod(), implementation_hash=content_hash('history'), source='proteus',
        capabilities=('run.journal', 'run.resume'), state_schema=EmptyOptions))
    registry.register(Plugin(ref=PluginRef(name='fixture.reader', version='1'), kind='stage', options=EmptyOptions,
        factory=lambda options: Reader(), implementation_hash=content_hash('reader'), source='proteus',
        capabilities=('stage.replay',), inputs={'history': 'proteus.evidence.view@1'}, outputs={'response': 'proteus.text@1'}))
    stage = StageSpec(id='inspect', role='proposer', operator=plugin('fixture.reader'),
        input_types={'history': 'proteus.evidence.view@1'}, output_types={'response': 'proteus.text@1'},
        read_scopes=('proteus.evidence.view@1',), write_scopes=('proteus.text@1',),
        evidence=access or EvidenceAccess(evaluation_ids=('score',), splits=('train',)))
    recipe = registry.resolve(replace(recipe, method=plugin('fixture.history'), stages=(stage,),
        actors={'proposer': ActorSpec(runtime=plugin('proteus.echo'))}))
    services = RunServices(run_id='run', recipe=recipe, registry=registry, root=tmp_path / 'run')
    if copy_artifacts:
        services.artifacts.copy_from(store, recipe.subject.initial_artifact)
    return services, store, dataset


def capture(services, **kwargs):
    ref = services.evidence_view('inspect', **kwargs)
    return ref, services.values.get(ref), EvidenceService(services).store


def test_full_history_has_multiple_codes_diagnostics_and_searchable_files(tmp_path):
    services, _, _ = fixture(tmp_path)
    first = services.initial_candidate(candidate_id='initial/portable?')
    services.evaluate(first, 'score')
    before_ref, before, store = capture(services)
    code = b'import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({"output": "NEW_CODE"}))\n'
    second = services.fork_candidate(first, candidate_id='new',
        delta=FileDelta(base_hash=first.artifact.content_hash, put=(FileWrite.from_bytes('main.py', code),)))
    services.evaluate(second, 'score')
    invalid = services.fork_candidate(first, candidate_id='invalid', validation=ValidationStatus.INVALID)
    services.evaluate(invalid, 'score')
    ref, view, store = capture(services)
    assert view.candidate_ids == (first.id, 'new', 'invalid')
    assert before.candidate_ids == (first.id,) and store.get(before_ref) == before
    assert {entry.kind for entry in view.entries} == set(view.access.kinds)
    assert len({entry.content_hash for entry in view.entries if entry.kind == 'artifact'}) == 2
    assert any('invalid' in store.read(ref, entry.path).decode().lower()
               for entry in view.entries if entry.kind == 'feedback' and entry.candidate_id == invalid.id)
    assert store.search(ref, 'NEW_CODE')
    result = services.stage('inspect', {'history': ref})
    assert result.status == OperationStatus.COMPLETED
    files = json.loads(services.values.get(result.outputs['response']).text)
    assert len(files) == 3 and sum('NEW_CODE' in code for code in files.values()) == 1
    assert services.requests[-1].inputs['history'] == ref
    export = store.materialize(ref, tmp_path / 'export')
    store.verify_materialized(ref, export)
    path = export / next(entry.path for entry in view.entries if entry.kind == 'artifact')
    path.chmod(0o644)
    path.write_text('edited copy')
    with pytest.raises(ContractError, match='bytes differ'):
        store.verify_materialized(ref, export)
    assert store.verify(ref) == view
    with pytest.raises(ContractError, match='fresh destination'):
        store.materialize(before_ref, export)
    for path in ('../private', '/etc/passwd', 'view.json', 'not-in-view'):
        with pytest.raises(ContractError, match='outside this evidence view'):
            store.read(ref, path)


@pytest.mark.parametrize('timing', ['after_prediction', 'after_batch'])
def test_feedback_release_timing_and_hidden_evaluations_never_export(tmp_path, timing):
    services, _, _ = fixture(tmp_path, timing=timing, references=('SECRET_REFERENCE', 2))
    spec = services.recipe.evaluations[0]
    hidden = replace(spec, id='heldout', purpose='test', visibility=FeedbackVisibility.HIDDEN,
                     feedback_timing='never', selection_eligible=False)
    services.recipe = replace(services.recipe, evaluations=(spec, hidden))
    first = services.initial_candidate()
    session = services.open_evaluation('score')
    session.step(first)
    ref, view, store = capture(services)
    assert bool([entry for entry in view.entries if entry.kind == 'feedback']) == (timing == 'after_prediction')
    assert not any(entry.kind == 'evaluation' for entry in view.entries)
    assert len([entry for entry in view.entries if entry.kind == 'rollout']) == 1
    assert all('SECRET_REFERENCE' not in store.read(ref, entry.path).decode()
               for entry in view.entries if entry.kind != 'feedback')
    if timing == 'after_prediction':
        assert any('SECRET_REFERENCE' in store.read(ref, entry.path).decode()
                   for entry in view.entries if entry.kind == 'feedback')
    session.step(first)
    session.finish()
    released_ref, released, _ = capture(services)
    assert len([entry for entry in released.entries if entry.kind == 'feedback']) == 4
    assert len([entry for entry in released.entries if entry.kind == 'evaluation']) == 1
    services.evaluate(first, 'heldout')
    final_ref, final, _ = capture(services)
    assert final_ref == released_ref and final == released
    assert all(entry.evaluation_id in (None, 'score') for entry in final.entries)
    assert store.get(ref) == view  # old views cannot grow when new feedback is released


def test_evidence_access_rejects_hidden_unknown_and_undeclared_scopes(tmp_path):
    services, _, _ = fixture(tmp_path)
    recipe, stage = services.recipe, services.recipe.stages[0]
    for access in (EvidenceAccess(evaluation_ids=('missing',), splits=('train',)),
                   EvidenceAccess(evaluation_ids=('score',), splits=('test',)),
                   EvidenceAccess(evaluation_ids=('score',), splits=('train', 'test'))):
        with pytest.raises(ContractError):
            replace(recipe, stages=(replace(stage, evidence=access),))
    with pytest.raises(ContractError, match='private evaluations'):
        replace(recipe, evaluations=(replace(recipe.evaluations[0], visibility=FeedbackVisibility.HIDDEN,
                                              feedback_timing='never'),))
    with pytest.raises(ContractError, match='explicit evidence view input'):
        replace(recipe, stages=(replace(stage, input_types={}),))
    services.recipe = replace(recipe, stages=(replace(stage, evidence=None),))
    with pytest.raises(ContractError, match='no declared evidence access'):
        capture(services)
    assert not (services.root / 'evidence').exists()
    assert not (services.root / 'private/ledger.sqlite3').exists()


def test_failed_batch_does_not_disclose_partially_produced_grades(tmp_path, monkeypatch):
    services, _, _ = fixture(tmp_path, references=('UNRELEASED_REFERENCE', 2))
    candidate = services.initial_candidate()
    session = services.open_evaluation('score')
    session.step(candidate)
    session.step(candidate)
    before, _, store = capture(services)
    grade = session._grade

    def fail_second(index):
        if index == 1:
            raise RuntimeError('injected failure during batch finalization')
        return grade(index)

    monkeypatch.setattr(session, '_grade', fail_second)
    with pytest.raises(RuntimeError, match='injected failure'):
        session.finish()
    assert any('UNRELEASED_REFERENCE' in value.to_json() for ref, value in services.values._values.values()
               if ref.kind == 'proteus.eval.feedback@1')
    after, view, _ = capture(services)
    assert before == after and services.evaluations == []
    assert not any(entry.kind in ('feedback', 'evaluation') for entry in view.entries)
    assert not store.search(after, 'UNRELEASED_REFERENCE')


def test_executable_example_and_completed_resume():
    from check_evidence_package import check
    check()


def test_candidate_and_kind_narrowing_and_role_binding(tmp_path):
    access = EvidenceAccess(evaluation_ids=(), splits=(), kinds=('artifact',))
    services, _, _ = fixture(tmp_path, access=access)
    stage = services.recipe.stages[0]
    services.recipe = replace(services.recipe, stages=(stage, replace(stage, id='other', role='reviewer')),
        actors={**services.recipe.actors, 'reviewer': ActorSpec(runtime=plugin('proteus.echo'))})
    first = services.initial_candidate()
    services.fork_candidate(first, candidate_id='second')
    ref, view, store = capture(services, candidate_ids=('second',))
    assert view.candidate_ids == ('second',) and len(view.entries) == 1
    assert view.entries[0].kind == 'artifact'
    for ids in (('missing',), ('second', 'second'), ['second']):
        with pytest.raises(ContractError):
            capture(services, candidate_ids=ids)
    empty_ref, empty, _ = capture(services, candidate_ids=())
    assert empty.entries == () and empty.candidate_ids == ()
    with pytest.raises(ContractError, match='another run, stage, role or policy'):
        services.stage('other', {'history': ref})
    result = services.stage('inspect', {'history': ref})
    context = StageContext(services, stage, services.requests[-1])
    with pytest.raises(ContractError, match='not supplied'):
        context.search_evidence(empty, 'any')
    assert result.status == OperationStatus.COMPLETED
    services.lock_hash = content_hash('another-lock')
    with pytest.raises(ContractError, match='another run, stage, role or policy'):
        services.stage('inspect', {'history': ref})
    assert store.get(empty_ref) == empty


def test_store_atomic_idempotent_publication_and_tamper_detection(tmp_path):
    services, _, _ = fixture(tmp_path)
    services.initial_candidate()
    ref, view, store = capture(services)
    files = {entry.path: store.read(ref, entry.path) for entry in view.entries}
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: store.put(view, files), range(12))) == [ref] * 12
    assert store.get(ref) == EvidenceView.from_json(view.to_json())
    bad = {**files, view.entries[0].path: b'corrupt'}
    with pytest.raises(ContractError, match='bytes differ'):
        store.put(view, bad)
    blob = store._path('blobs', view.entries[0].content_hash)
    blob.chmod(0o644)
    blob.write_bytes(b'corrupt')
    with pytest.raises(ContractError, match='integrity mismatch'):
        services.stage('inspect', {'history': ref})
    assert not services.results[-1].outputs.get('response')


@pytest.mark.parametrize('missing', ['views', 'blobs'])
def test_restoring_snapshot_rejects_missing_evidence_even_for_a_completed_run(tmp_path, missing):
    services, _, _ = fixture(tmp_path)
    services.initial_candidate()
    ref, view, store = capture(services)
    snapshot = json.loads(canonical_json(services.snapshot()))
    digest = ref.content_hash if missing == 'views' else view.entries[0].content_hash
    store._path(missing, digest).unlink()
    restored = RunServices(run_id=services.run_id, recipe=services.recipe, registry=services.registry,
                           root=services.root, lock_hash=services.lock_hash, resume=True)
    with pytest.raises(FileNotFoundError):
        restored.restore(snapshot)


WORKER = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_v1_evidence import fixture
from test_v1_evaluation import SOURCE
from proteus.core.recovery import Journal
from proteus.evidence.store import EvidenceStore
from proteus.recipes.runner import run_recipe
root, boundary, resume = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == 'resume'
root.mkdir(parents=True, exist_ok=True)
marker = root / 'calls'
code = 'from pathlib import Path\nwith Path(' + repr(str(marker)) + ').open("a") as f: f.write("call\\n")\n'
code += 'import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps({"output":1}))\n'
services, store, _ = fixture(root, code=code, copy_artifacts=False)
recipe, registry = services.recipe, services.registry
if not resume:
    if boundary == 'store':
        put = EvidenceStore.put
        def saved(self, *args):
            result = put(self, *args)
            os._exit(86)
        EvidenceStore.put = saved
    else:
        call = Journal.call
        def recorded(self, kind, *args, **kwargs):
            result = call(self, kind, *args, **kwargs)
            if kind == 'evidence.view': os._exit(86)
            return result
        Journal.call = recorded
lock = registry.make_lock(recipe, [SOURCE])
result = run_recipe(recipe, registry=registry, lock=lock, root=root/'run', resume=resume, artifact_source=store.root)
(root / 'result.json').write_text(result.to_json())
assert result.status.value == 'completed', result.diagnostic
'''


@pytest.mark.parametrize('boundary', ['store', 'journal'])
def test_hard_exit_recovery_preserves_disclosed_view_without_duplicate_evaluations(tmp_path, boundary):
    command = [sys.executable, '-c', WORKER, str(tmp_path), boundary]
    first = subprocess.run([*command, 'start'], capture_output=True, text=True, timeout=40)
    assert first.returncode == 86, first.stderr
    assert (tmp_path / 'calls').read_text().splitlines() == ['call'] * 2
    resumed = subprocess.run([*command, 'resume'], capture_output=True, text=True, timeout=40)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert (tmp_path / 'calls').read_text().splitlines() == ['call'] * 4
    result = MethodResult.from_json((tmp_path / 'result.json').read_text())
    store = EvidenceStore(tmp_path / 'run/evidence')
    before, after = (store.verify(result.outputs[key]) for key in ('before', 'after'))
    assert before.candidate_ids == ('initial',) and after.candidate_ids == ('initial', 'second')
    assert len([entry for entry in before.entries if entry.kind == 'rollout']) == 2
    assert len([entry for entry in after.entries if entry.kind == 'rollout']) == 4
    again = subprocess.run([*command, 'resume'], capture_output=True, text=True, timeout=40)
    assert again.returncode == 0, again.stderr
    assert (tmp_path / 'calls').read_text().splitlines() == ['call'] * 4
