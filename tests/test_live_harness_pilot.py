"""Seed projection regression for the live Pi/DSH acceptance recipe."""

import importlib.util
from pathlib import Path

import pytest

from proteus.artifacts.store import ArtifactStore
from proteus.core.serialization import ContractError


@pytest.fixture
def pilot():
    path = Path(__file__).parents[1] / 'recipes/pilots/harness-roles/run.py'
    spec = importlib.util.spec_from_file_location('live_harness_role_pilot', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_aliases(root, pilot):
    source = root / 'src'
    for relative, target in pilot.DSH_ALIASES.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        destination = (path.parent / target).resolve()
        if relative == '.claude/skills':
            destination.mkdir(parents=True)
            (destination / 'SKILL.md').write_text('skill bytes')
        else:
            destination.write_text(relative)
        path.symlink_to(target)
    return source


def test_dsh_seed_alias_projection_preserves_contents_without_links(tmp_path, pilot):
    source = seed_aliases(tmp_path, pilot)
    expected = {p: (source / p).read_bytes() for p in pilot.DSH_ALIASES if p != '.claude/skills'}
    changes = pilot.normalize_seed_aliases(tmp_path, backend='dsh')
    files = ArtifactStore.read_tree(tmp_path)
    assert len(changes) == 8
    for path, content in expected.items():
        assert files['src/' + path].data == content
    assert files['src/.claude/skills/SKILL.md'].data == b'skill bytes'


def test_dsh_seed_alias_projection_rejects_source_escape(tmp_path, pilot):
    source = seed_aliases(tmp_path / 'seed', pilot)
    target = source / '.agents/notes/implemented/AGENTS.md'
    target.unlink()
    outside = tmp_path / 'outside'
    outside.write_text('must not import')
    target.symlink_to(outside)
    with pytest.raises(ContractError, match='escapes'):
        pilot.normalize_seed_aliases(tmp_path / 'seed', backend='dsh')


def test_unrecognized_links_remain_rejected(tmp_path, pilot):
    source = seed_aliases(tmp_path, pilot)
    (source / 'unknown').symlink_to('AGENTS.md')
    pilot.normalize_seed_aliases(tmp_path, backend='dsh')
    with pytest.raises(ContractError, match='only regular files'):
        ArtifactStore.read_tree(tmp_path)


@pytest.mark.parametrize('method', ('ace', 'combee-ace', 'meta', 'coral'))
@pytest.mark.parametrize('backend', ('pi', 'dsh', 'codex'))
def test_additional_pilots_preflight_real_roles_and_lock_without_inference(tmp_path, method, backend):
    from proteus.recipes.manifest import SourcePin
    path = Path(__file__).parents[1] / 'recipes/pilots/harness-roles/other_methods.py'
    spec = importlib.util.spec_from_file_location('additional_harness_pilots', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry, store, recipe = module.prepare(tmp_path / method, method=method,
        model='deepseek-v4-flash', backend=backend, image='sha256:' + '1' * 64,
        harness_model='gpt-5.4-mini' if backend == 'codex' else None)
    source = SourcePin(name='proteus', repository='https://github.com/proteus-evolve/Proteus', revision='1' * 40)
    registry.make_lock(recipe, [source])
    assert all(not actor.runtime.plugin.name.startswith('example.') for actor in recipe.actors.values())
    assert any(e.visibility.value == 'hidden' for e in recipe.evaluations)
    assert not (tmp_path / method / 'run').exists()
    if method in ('ace', 'combee-ace'):
        assert recipe.actors['generator'].runtime.plugin.name == f'proteus.harness.{backend}'
        assert recipe.actors['generator'].model == ('gpt-5.4-mini' if backend == 'codex' else 'deepseek-v4-flash')
        assert recipe.actors['reflector'].model == 'deepseek-v4-flash'
        assert recipe.subject.initial_artifact.codec.name == 'proteus.ace_playbook'
    elif method == 'meta':
        assert recipe.actors['proposer'].runtime.plugin.name == f'proteus.workspace.{backend}'
        assert recipe.subject.runner.plugin.name == 'proteus.code_subject'
    else:
        assert recipe.workers[0].runtime.plugin.name == 'proteus.worker.command'
        assert 'coral_worker.py' in recipe.workers[0].runtime.options['command'][1]
        assert recipe.workers[0].runtime.options['credential_env'] == (
            ('CODEX_API_KEY',) if backend == 'codex' else ('DEEPSEEK_API_KEY',))
