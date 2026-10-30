"""Reject corrupt or source-mismatched retained runtime artifacts before reuse."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def reuse(tmp_path):
    path = Path(__file__).parents[1] / 'recipes/pilots/harness-roles/reuse_codex.py'
    spec = importlib.util.spec_from_file_location('codex_pilot_reuse', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    artifact = tmp_path / 'producer'
    accepted = artifact / 'live-ace'
    accepted.mkdir(parents=True)
    producer = {'id': 123, 'head_sha': 'a' * 40, 'status': 'completed',
        'event': 'workflow_dispatch', 'path': '.github/workflows/native-source-integration.yml'}
    image = {'Id': 'sha256:' + 'b' * 64,
        'Config': {'Labels': {'org.opencontainers.image.revision': module.CODEX_REF}}}
    (artifact / 'image.json').write_text(json.dumps([image]))
    source = 'c' * 64
    retained = artifact / 'live-meta/run/workspace/.codex-builds' / source
    retained.mkdir(parents=True)
    (retained / 'source.sha256').write_text(source + '\n')
    entries = []
    for name in module.BINARIES:
        file = retained / name
        file.write_bytes(('fake test binary ' + name).encode())
        entries.append({'path': 'run/.codex-builds/' + source + '/' + name,
            'sha256': module.digest(file), 'size_bytes': file.stat().st_size, 'source_hash': source})
    evidence = {'status': 'passed', 'completed_resume_new_dispatches': 0,
        'proteus_head': producer['head_sha'], 'backend': 'codex',
        'source_revision': module.CODEX_REF, 'image_id': image['Id'],
        'pristine_source_sha256': source}
    (accepted / 'evidence.json').write_text(json.dumps(evidence))
    (accepted / 'omitted-runtime-binaries.json').write_text(json.dumps({
        'source_revision': module.CODEX_REF, 'image_id': image['Id'], 'binaries': entries}))
    return module, artifact, producer, tmp_path / 'output', retained


def test_reuse_preserves_verified_bytes_and_provenance(reuse):
    module, artifact, producer, output, retained = reuse
    report = module.prepare(artifact, producer, output)
    assert report['producer_head'] == producer['head_sha']
    assert report['accepted_anchor_cells'] == ['live-ace']
    for name in module.BINARIES:
        assert (output / name).read_bytes() == (retained / name).read_bytes()
    assert (output / 'source.expected').read_text().strip() == retained.name


@pytest.mark.parametrize('damage', ('bytes', 'anchor', 'source', 'head', 'image', 'workflow'))
def test_reuse_rejects_unverifiable_publication_before_copy(reuse, damage):
    module, artifact, producer, output, retained = reuse
    if damage == 'bytes':
        (retained / 'codex').write_bytes(b'corrupt')
    elif damage == 'anchor':
        (artifact / 'live-ace/omitted-runtime-binaries.json').unlink()
    elif damage == 'source':
        (retained / 'source.sha256').write_text('d' * 64)
    elif damage == 'head':
        producer['head_sha'] = 'e' * 40
    elif damage == 'image':
        file = artifact / 'image.json'
        value = json.loads(file.read_text())
        value[0]['Id'] = 'sha256:' + 'f' * 64
        file.write_text(json.dumps(value))
    else:
        producer['path'] = '.github/workflows/other.yml'
    with pytest.raises(ValueError):
        module.prepare(artifact, producer, output)
    assert not output.exists()
