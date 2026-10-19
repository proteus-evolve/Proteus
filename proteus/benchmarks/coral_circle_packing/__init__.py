"""The pinned CORAL circle-packing domain, with independent numeric verification."""

from pathlib import Path
import hashlib
import json

from proteus.core.contracts import require

ROOT = Path(__file__).parent
REVISION = '0123dfb939b35228cf2c1fde224cd0561e727408'


def reference():
    record = json.loads((ROOT/'reference.json').read_text())
    require(record['revision'] == REVISION, 'circle-packing reference revision differs')
    for name, item in record['files'].items():
        require('sha256:'+hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == item['sha256'],
                'circle-packing reference resource changed: '+name)
    return record
