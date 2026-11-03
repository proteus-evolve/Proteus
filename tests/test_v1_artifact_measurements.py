"""Measurements retain their units, recorded ancestry and saved-byte provenance."""
from dataclasses import replace
import json

import pytest

from proteus.api.v1 import (
    ArtifactStore, Candidate, ContractError, EmptyOptions, FileContent, MeasurementSpec,
    MeasurementSurface, Plugin, PluginRef, StructuredRecords, TextComponents, builtin_registry, content_hash,
)
from proteus.artifacts.codecs import FileTreeCodec
from proteus.measure.artifacts import measure_lineage
from proteus.recipes.export import report_run
from check_recipe_catalog import check_example


def candidate(store, name, value, parents=(), codec='proteus.text_components'):
    if codec == 'proteus.text_components':
        value = TextComponents(components=value)
    elif codec == 'proteus.structured_records':
        value = StructuredRecords(records=value)
    artifact = store.put(PluginRef(name=codec, version='1'), value)
    return Candidate(id=name, run_id='measurement', artifact=artifact, parent_ids=parents,
                     created_by='fixture', created_attempt_id=name)


def test_branch_and_merge_are_per_parent_and_travel_requires_a_declared_path(tmp_path):
    store = ArtifactStore(tmp_path, builtin_registry())
    root = candidate(store, 'root', {'keep':'x', 'rev':'old', 'drop':'z'})
    a = candidate(store, 'a', {'keep':'x', 'rev':'new', 'add':'y'}, ('root',))
    b = candidate(store, 'b', {'keep':'x', 'rev':'old', 'drop':'z'}, ('root',))
    merged = candidate(store, 'merge', {'keep':'x', 'rev':'new', 'add':'y', 'extra':'v'}, ('a','b'))
    report = measure_lineage([merged, b, a, root], store,
        MeasurementSpec(lineage_paths={'via-a':('root','a','merge'), 'via-b':('root','b','merge')}))
    edges = {(row['parent_id'],row['candidate_id']):row for row in report['edges']}
    assert len(edges) == 4 and not any(row['candidate_id']=='root' for row in report['edges'])
    edge = edges[('root','a')]
    assert [edge[k] for k in ('added','dropped','revised','unchanged','union')] == [1,1,1,1,4]
    assert edge['distance'] == .75 and edge['churn'] == pytest.approx(2/3)
    assert edges[('root','b')]['distance'] == 0
    paths = {row['path']:row for row in report['paths']}
    assert paths['via-a']['edge_distance_sum'] == 1 and paths['via-b']['edge_distance_sum'] == .8
    assert [paths['via-a'][k] for k in ('added','dropped','revised','unchanged')] == [2,1,1,4]
    assert report == measure_lineage([root, a, b, merged], store,
        MeasurementSpec(lineage_paths={'via-b':('root','b','merge'), 'via-a':('root','a','merge')}))
    assert measure_lineage([root,a,b,merged],store,MeasurementSpec())['paths'] == []
    with pytest.raises(ContractError, match='parent edges'):
        measure_lineage([root,a,b,merged],store,MeasurementSpec(lineage_paths={'invented':('a','b')}))


def test_record_identity_and_canonical_content_are_not_a_text_distance(tmp_path):
    store = ArtifactStore(tmp_path, builtin_registry())
    root = candidate(store,'root',{'a':{'x':1,'y':2},'b':{'count':3}},codec='proteus.structured_records')
    child = candidate(store,'child',{'b':{'count':4},'a':{'y':2,'x':1}},('root',),'proteus.structured_records')
    row, = measure_lineage([root,child],store,MeasurementSpec())['edges']
    assert row['unit_kind'] == 'structured_record' and row['revised'] == row['unchanged'] == 1
    assert row['distance'] == .5


def test_file_surfaces_reuse_native_units_and_expose_syntax_fallback(tmp_path):
    store = ArtifactStore(tmp_path, builtin_registry())
    original = {'main.py':FileContent(b'def f():\n    return 1\n'),
                'skills/a/readme':FileContent(b'first'), 'skills/a/run':FileContent(b'unchanged'),
                'skills/b/readme':FileContent(b'second'), 'binary':FileContent(b'\xff')}
    root = candidate(store,'root',original,codec='proteus.file_tree')
    child = candidate(store,'child',{**original,'main.py':FileContent(b'def f():\n    return 2\n\ndef g():\n    pass\n'),
        'skills/a/readme':FileContent(b'revised')},('root',),'proteus.file_tree')
    spec = MeasurementSpec(file_surfaces=(MeasurementSurface(name='code',subdir='main.py',unit='top_level_def'),
        MeasurementSurface(name='skills',subdir='skills',unit='directory'),
        MeasurementSurface(name='all',subdir='.'), MeasurementSurface(name='absent',subdir='missing')))
    report = measure_lineage([root,child],store,spec)
    edges = {row['surface']:row for row in report['edges']}
    assert edges['code']['revised'] == edges['code']['added'] == 1 and edges['code']['distance'] == 1
    assert edges['skills']['revised'] == edges['skills']['unchanged'] == 1 and edges['skills']['distance'] == .5
    assert edges['all']['distance'] is None and edges['all']['reason'] == 'surface_is_not_utf8'
    assert edges['absent']['distance'] == 0 and edges['absent']['union'] == 0
    assert all(not row['present'] for row in report['artifacts'] if row['surface']=='absent')
    broken = candidate(store,'broken',{'main.py':FileContent(b'def f(:\n')},('child',),'proteus.file_tree')
    report = measure_lineage([root,child,broken],store,spec)
    assert next(row for row in report['artifacts'] if row['candidate_id']=='broken' and row['surface']=='code')['syntax_fallback_files'] == ['main.py']
    assert all(row['unit_count'] is None for row in measure_lineage([root,child],store,MeasurementSpec())['artifacts'])


def test_unknown_codec_and_mixed_lineage_never_become_zero_or_execute_plugins(tmp_path):
    registry = builtin_registry()
    registry.register(Plugin(ref=PluginRef(name='outside.codec',version='1'),kind='codec',options=EmptyOptions,
        factory=lambda options:FileTreeCodec(),implementation_hash=content_hash('outside'),source='outside'))
    store = ArtifactStore(tmp_path,registry)
    root = candidate(store,'root',{'prompt':'hello'})
    foreign = candidate(store,'foreign',{'file':FileContent(b'custom')},('root',),'outside.codec')
    child = candidate(store,'child',{'prompt':'world'},('foreign',))
    reader = ArtifactStore(tmp_path,registry=None)
    report = measure_lineage([root,foreign,child],reader,MeasurementSpec(lineage_paths={'mixed':('root','foreign','child')}))
    assert next(row for row in report['artifacts'] if row['candidate_id']=='foreign')['reason'] == 'unsupported_codec'
    assert all(row['distance'] is None and row['reason']=='different_codecs' for row in report['edges'])
    assert all(row['edge_distance_sum'] is None for row in report['paths'])


@pytest.mark.parametrize('path', ['/absolute','../escape','a/../b','a//b','a/','a\\b'])
def test_surface_paths_cannot_escape_or_alias(path):
    with pytest.raises(ContractError):
        MeasurementSurface(name='unsafe',subdir=path)


def test_missing_cycle_duplicate_and_changed_artifact_ref_are_rejected(tmp_path):
    store = ArtifactStore(tmp_path,builtin_registry())
    root = candidate(store,'root',{'prompt':'hello'})
    child = candidate(store,'child',{'prompt':'hello'},('root',))
    for objects in ([child], [root,root], [replace(root,parent_ids=('child',)),child]):
        with pytest.raises(ContractError):
            measure_lineage(objects,store,MeasurementSpec())
    changed = replace(child,artifact=replace(root.artifact,codec=PluginRef(name='different',version='1')))
    with pytest.raises(ContractError,match='manifest'):
        measure_lineage([root,changed],store,MeasurementSpec())
    blob = next((tmp_path/'blobs').iterdir())
    blob.chmod(0o644)
    blob.write_bytes(b'corrupted')
    with pytest.raises(ContractError,match='corrupt'):
        measure_lineage([root],store,MeasurementSpec())


def test_file_measurement_never_executes_candidate_and_invalid_bytes_are_not_zero(tmp_path):
    store = ArtifactStore(tmp_path/'store',builtin_registry())
    marker = tmp_path/'executed'
    source = f'from pathlib import Path\nPath({str(marker)!r}).touch()\n'
    root = candidate(store,'a',{'main.py':FileContent(source.encode())},codec='proteus.file_tree')
    child = candidate(store,'b',{'main.py':FileContent(b'\xff')},('a',),'proteus.file_tree')
    spec = MeasurementSpec(file_surfaces=(MeasurementSurface(name='code',subdir='main.py',unit='top_level_def'),),
                           lineage_paths={'incomplete':('a','b')})
    report = measure_lineage([root,child],store,spec)
    assert not marker.exists() and report['edges'][0]['distance'] is None
    assert report['paths'][0]['edge_distance_sum'] is None
    changed = replace(child,artifact=replace(root.artifact,codec=PluginRef(name='different',version='1')))
    with pytest.raises(ContractError,match='manifest'):
        measure_lineage([root,changed],store,spec)


def test_measurement_export_is_recomputed_read_only_and_keeps_hidden_grades_hidden(tmp_path):
    check_example(tmp_path,'search-policy')
    root = tmp_path/'run'
    before = {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    spec = MeasurementSpec(file_surfaces=(MeasurementSurface(name='code',subdir='main.py',unit='top_level_def'),))
    report = report_run(root,measurement_spec=spec)
    assert len(report['measurements']['artifacts']) == 3 and len(report['measurements']['edges']) == 2
    assert all(row['distance'] is not None for row in report['measurements']['edges'])
    assert all(row['score'] is None for row in report['tasks'] if row['visibility']=='hidden')
    assert report['report_hash'] == content_hash({k:v for k,v in report.items() if k!='report_hash'})
    assert before == {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert json.loads(spec.to_json()) == report['measurements']['spec']
