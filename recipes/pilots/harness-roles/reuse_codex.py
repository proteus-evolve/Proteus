"""Recover fixed Codex binaries from a source-bound pilot artifact, without compiling.

Only a retained failed-cell publication matching successful-cell hash manifests
is accepted. This image is for fixed actor/workspace roles, not source evolution.
Download the producer artifact on the CI runner, never onto a space-limited host.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

CODEX_REF = 'a7b86b6201d35200280e3d24ca5ebccd8122d6ca'
BINARIES = ('codex', 'codex-code-mode-host')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def prepare(artifact, producer, output):
    if producer['path'] != '.github/workflows/native-source-integration.yml':
        raise ValueError('Unexpected producer workflow')
    if producer['status'] != 'completed' or producer['event'] != 'workflow_dispatch':
        raise ValueError('Producer must be a completed explicit pilot run')
    image = json.loads((artifact / 'image.json').read_text())[0]
    if image['Config']['Labels']['org.opencontainers.image.revision'] != CODEX_REF:
        raise ValueError('Producer image uses a different upstream pin')
    anchors = {}
    source_hashes = set()
    witnesses = []
    for manifest_file in sorted(artifact.glob('live-*/omitted-runtime-binaries.json')):
        root = manifest_file.parent
        evidence = json.loads((root / 'evidence.json').read_text())
        manifest = json.loads(manifest_file.read_text())
        if (evidence['status'] != 'passed' or evidence['completed_resume_new_dispatches'] != 0
                or evidence['proteus_head'] != producer['head_sha']
                or evidence['backend'] != 'codex'
                or evidence['source_revision'] != CODEX_REF
                or manifest['source_revision'] != CODEX_REF
                or evidence['image_id'] != image['Id'] or manifest['image_id'] != image['Id']):
            raise ValueError('Accepted-cell provenance does not match producer')
        source_hashes.add(evidence['pristine_source_sha256'])
        witnesses.append(root.name)
        for item in manifest['binaries']:
            name = Path(item['path']).name
            if name not in BINARIES or item['source_hash'] != evidence['pristine_source_sha256']:
                raise ValueError('Unexpected manifest binary or source hash')
            anchor = (item['sha256'], item['size_bytes'])
            if name in anchors and anchors[name] != anchor:
                raise ValueError('Conflicting binary anchors')
            anchors[name] = anchor
    if set(anchors) != set(BINARIES) or len(source_hashes) != 1:
        raise ValueError('Missing consistent accepted-cell binary anchors')
    source_hash = source_hashes.pop()
    publications = sorted(artifact.glob('live-*/run/**/.codex-builds/' + source_hash))
    retained = next((p for p in publications if all((p / name).is_file() for name in BINARIES)), None)
    if retained is None or (retained / 'source.sha256').read_text().strip() != source_hash:
        raise ValueError('No retained matching binary publication')
    for name in BINARIES:
        path = retained / name
        if path.is_symlink() or (digest(path), path.stat().st_size) != anchors[name]:
            raise ValueError('Retained binary does not match accepted-cell anchor: ' + name)
    output.mkdir(parents=True, exist_ok=False)
    for name in BINARIES:
        shutil.copyfile(retained / name, output / name)
        (output / name).chmod(0o755)
    (output / 'source.expected').write_text(source_hash + '\n')
    report = {'producer_run_id': producer['id'], 'producer_head': producer['head_sha'],
        'producer_image_id': image['Id'], 'upstream_revision': CODEX_REF,
        'pristine_source_sha256': source_hash, 'accepted_anchor_cells': witnesses,
        'retained_publication': retained.relative_to(artifact).as_posix(),
        'binary_anchors': {name: {'sha256': value[0], 'size_bytes': value[1]}
                           for name, value in anchors.items()},
        'scope': 'Fixed-runtime recovery; no Rust build cache or source evolution support'}
    (output / 'reuse.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact', type=Path)
    parser.add_argument('producer', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    prepare(args.artifact, json.loads(args.producer.read_text()), args.output)
