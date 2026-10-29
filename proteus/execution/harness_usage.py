"""Bind generic harness actor accounting to its private native receipts."""
from dataclasses import replace
from pathlib import Path
import re

from proteus.core.contracts import Usage, require
from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.native_usage import DIMENSIONS, summarize_native_inputs
from proteus.core.serialization import content_hash, json_value


INFERENCE = DIMENSIONS[:-1]


def collect(workspace, backend, *, episode=1, phases=('act',)):
    records = read_native_inputs(workspace)
    if not records:
        return None, None, records
    require(all(row['input']['backend'] == backend and row['input']['episode'] == episode
                and row['input']['phase'] in phases for row in records),
            'generic actor receipt belongs to another dispatch')
    summary = summarize_native_inputs(records)
    usage = replace(Usage.from_dict(summary['reported']), wall_seconds=None)
    marker = {'version': 1, 'backend': backend, 'records_hash': content_hash(records),
              'boundary_count': len(records), 'inference': {key: getattr(usage, key) for key in INFERENCE}}
    if episode != 1 or phases != ('act',):
        marker.update(version=2, episode=episode, phases=list(phases))
    return usage, marker, records


def verify(root, record):
    """Validate a settled actor's binding, independently of its mutable workspace."""
    result = record.get('result')
    marker = result.get('runtime', {}).get('settings', {}).get('native_usage') if result else None
    if marker is None:
        return []
    require(isinstance(record.get('id'), str) and re.fullmatch(r'actor-[0-9a-f]{64}', record['id']) is not None,
            'invalid native actor receipt identity')
    require(isinstance(marker, dict) and type(marker.get('backend')) is str,
            'invalid native actor usage binding')
    workspace = Path(root).resolve()/'actors'/record['id']
    directory = input_directory(workspace)
    require(all(not path.is_symlink() for path in (workspace.parent, directory.parent, directory)),
            'native actor evidence directory cannot be a symlink')
    require(marker.get('version') in (1, 2), 'unsupported harness usage binding')
    usage, expected, records = collect(workspace, marker['backend'],
        episode=marker.get('episode', 1), phases=tuple(marker.get('phases', ('act',))))
    require(expected is not None and json_value(marker) == expected, 'native actor usage evidence differs')
    for reported in (result.get('usage'), record.get('usage')):
        if reported is not None:
            require(all(reported.get(key) == getattr(usage, key) for key in INFERENCE),
                    'native actor usage differs from its receipts')
    return records
