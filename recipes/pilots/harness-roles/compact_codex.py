"""Replace repeated fixed-runtime binaries with hashes AFTER completed-resume checks.

Candidate files, source trees, provider traces, usage and dispatch receipts remain.
This compact archive is for audit; it is not a restartable working directory.
"""
import hashlib
import json
from pathlib import Path
import sys


def compact(root):
    evidence = json.loads((root / 'evidence.json').read_text())
    assert evidence['status'] == 'passed' and evidence['completed_resume_new_dispatches'] == 0
    entries = []
    for directory in root.rglob('.codex-builds'):
        for path in directory.glob('*/*'):
            if path.name not in ('codex', 'codex-code-mode-host') or not path.is_file() or path.is_symlink():
                continue
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            entries.append({'path': path.relative_to(root).as_posix(), 'size_bytes': path.stat().st_size,
                'sha256': digest, 'source_hash': path.parent.name})
    manifest = {'purpose': 'Post-verification audit archive; repeated fixed-runtime binaries omitted.',
        'image_id': evidence['image_id'], 'source_revision': evidence['source_revision'], 'binaries': entries}
    (root / 'omitted-runtime-binaries.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for entry in entries:
        (root / entry['path']).unlink()


if __name__ == '__main__':
    compact(Path(sys.argv[1]))
