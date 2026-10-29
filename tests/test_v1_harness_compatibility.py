"""Optional new roles do not change the serialized shapes of old records."""

import json
from pathlib import Path

import pytest

from proteus.artifacts.store import ArtifactStore
from proteus.evaluation.records import Grade
from proteus.execution.workspace_records import WorkspaceInput
from proteus.recipes.config import EvaluationSpec


@pytest.mark.parametrize('name,schema', (('evaluation', EvaluationSpec), ('grade', Grade), ('workspace', WorkspaceInput)))
def test_actual_pre_role_record_roundtrips_with_original_fields_and_hash(name, schema):
    from proteus.core.serialization import content_hash
    data = json.loads((Path(__file__).parent / 'fixtures/harness-role-shapes-325ab5e.json').read_text())
    assert data['source_commit'] == '325ab5edb056f592209e23116e6ffabcb87ec9e8'
    old = data[name]
    record = schema.from_dict(old)
    assert record.to_dict() == old
    assert content_hash(record) == content_hash(old)


@pytest.mark.parametrize('name,directories', (('pi', ('notes', 'tools', 'skills')),
    ('dsh', ('notes', 'tools')), ('codex', ('notes', 'tools', '.agents/skills'))))
def test_source_harness_restores_only_empty_surface_directories(tmp_path, name, directories):
    from proteus.execution.actors import harness_class
    cls = harness_class(name)
    adapter = cls.__new__(cls)  # This hook is independent of model, image or authentication setup.
    (tmp_path / 'AGENTS.md').write_text('the optimized instructions must survive')
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src/candidate').write_text('exact source bytes')
    before = ArtifactStore.read_tree(tmp_path)
    adapter.prepare_candidate(tmp_path)
    adapter.prepare_candidate(tmp_path)
    assert ArtifactStore.read_tree(tmp_path) == before
    assert all((tmp_path / path).is_dir() for path in directories)
