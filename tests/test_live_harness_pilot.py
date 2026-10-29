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
