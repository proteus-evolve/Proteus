"""Durable controller intervals; never infer active time across process exits."""

import math
from pathlib import Path
from time import monotonic, time_ns

from proteus.artifacts.store import publish_bytes
from proteus.core.contracts import require
from proteus.core.serialization import canonical_json, content_hash, read_json


DIRECTORY = 'private/controller-timing'


def _publish(path, value):
    value = {**value, 'hash': content_hash(value)}
    publish_bytes(path, (canonical_json(value)+'\n').encode())
    return value


def read_intervals(root, manifest_hash, read):
    """Validate append-only starts, outcomes and their immutable execution snapshots."""
    directory = Path(root)/DIRECTORY
    starts = sorted(directory.glob('*.start.json'))
    require({p.stem.removesuffix('.finish') for p in directory.glob('*.finish.json')}
            <= {p.stem.removesuffix('.start') for p in starts}, 'orphan controller timing outcome')
    rows, previous, checked = [], None, set()
    for sequence, path in enumerate(starts, 1):
        require(path.name == f'{sequence:08d}.start.json' and not path.is_symlink(),
                'controller timing sequence differs')
        start = read(f'{DIRECTORY}/{path.name}')
        require(start.get('hash') == content_hash({k:v for k,v in start.items() if k != 'hash'})
                and start.get('version') == 1 and start.get('sequence') == sequence
                and start.get('manifest_hash') == manifest_hash and start.get('previous') == previous
                and type(start.get('resume')) is bool, 'controller timing start differs')
        reference = {'sequence': sequence, 'start_hash': start['hash']}
        finish = None
        end = directory/f'{sequence:08d}.finish.json'
        require(not end.is_symlink(), 'invalid controller timing outcome path')
        if end.exists():
            finish = read(f'{DIRECTORY}/{end.name}')
            duration = finish.get('wall_seconds')
            require(finish.get('hash') == content_hash({k:v for k,v in finish.items() if k != 'hash'})
                    and finish.get('version') == 1 and finish.get('start') == reference
                    and type(duration) in (int, float) and math.isfinite(duration) and duration >= 0
                    and type(finish.get('replayed_result')) is bool
                    and finish.get('status') in (None, 'completed', 'failed', 'cancelled')
                    and (finish.get('error_type') is None or isinstance(finish['error_type'], str)),
                    'controller timing outcome differs')
            snapshot = finish.get('snapshot')
            if snapshot is not None:
                require(isinstance(snapshot, str) and Path(snapshot).name == snapshot
                        and snapshot.startswith('execution-') and snapshot.endswith('.json'),
                        'controller timing snapshot path differs')
                data = read(snapshot)
                require(data.get('controller_timing') == reference and data.get('manifest_hash') == manifest_hash
                        and content_hash(data) == finish.get('snapshot_hash'), 'controller timing snapshot differs')
                require(snapshot not in checked, 'controller timing snapshot reused')
                checked.add(snapshot)
            else:
                require(finish.get('snapshot_hash') is None and finish.get('status') is None,
                        'controller timing status has no snapshot')
        rows.append({'start': start, 'finish': finish})
        previous = start['hash']
    references = [{'sequence': row['start']['sequence'], 'start_hash': row['start']['hash']} for row in rows]
    for path in sorted(Path(root).glob('execution-*.json')):
        if path.name in checked:
            continue
        data = read(path.name)
        reference = data.get('controller_timing')
        require(reference is None or (reference in references and data.get('manifest_hash') == manifest_hash),
                'execution references missing controller timing start')
    return rows


class ControllerInvocation:
    """Caller holds the run lease. Publication is outside the replay journal."""

    def __init__(self, root, manifest_hash, *, resume):
        self.root = Path(root)
        self.manifest_hash = manifest_hash
        self.resume = resume
        self.snapshot = None
        self.status = None
        self.replayed_result = False

    def __enter__(self):
        self.beginning = monotonic()
        rows = read_intervals(self.root, self.manifest_hash,
                             lambda name: read_json((self.root/name).read_text(encoding='utf-8')))
        self.sequence = len(rows)+1
        start = _publish(self.root/DIRECTORY/f'{self.sequence:08d}.start.json', {
            'version': 1, 'sequence': self.sequence, 'manifest_hash': self.manifest_hash,
            'previous': rows[-1]['start']['hash'] if rows else None,
            'resume': self.resume, 'started_ns': time_ns()})
        self.reference = {'sequence': self.sequence, 'start_hash': start['hash']}
        return self

    def __exit__(self, exc_type, exc, traceback):
        snapshot = read_json((self.root/self.snapshot).read_text(encoding='utf-8')) if self.snapshot else None
        duration = monotonic()-self.beginning
        require(math.isfinite(duration) and duration >= 0, 'invalid controller elapsed measurement')
        _publish(self.root/DIRECTORY/f'{self.sequence:08d}.finish.json', {
            'version': 1, 'start': self.reference, 'wall_seconds': duration, 'finished_ns': time_ns(),
            'replayed_result': self.replayed_result, 'status': self.status,
            'snapshot': self.snapshot, 'snapshot_hash': content_hash(snapshot) if snapshot is not None else None,
            'error_type': exc_type.__name__ if exc_type else None})


def report_timing(run):
    rows = read_intervals(run.root, run.manifest.manifest_hash, run.read)
    reference = run.records.get('controller_timing')
    require((reference is None and not rows) or (rows and reference == {
        'sequence': len(rows), 'start_hash': rows[-1]['start']['hash']}),
        'latest execution controller timing differs or a newer invocation has no snapshot')
    # Snapshots bind the last start; a missing finish remains an unknown interval.
    # Old runs have no timing records. A first recorded resume has an unmeasured prefix.
    complete_prefix = bool(rows) and not rows[0]['start']['resume']
    intervals = []
    for row in rows:
        start, end = row['start'], row['finish']
        intervals.append({'invocation': start['sequence'], 'resume': start['resume'],
            'replayed_result': end['replayed_result'] if end else None,
            'wall_seconds': end['wall_seconds'] if end else None,
            'status': end['status'] if end else None, 'error_type': end['error_type'] if end else None,
            'snapshot': end['snapshot'] if end else None})
    work = [row for row in intervals if row['replayed_result'] is not True]
    known = [row['wall_seconds'] for row in work if row['wall_seconds'] is not None]
    complete = complete_prefix and len(known) == len(work)
    continuous = (complete and len(work) == 1 and not work[0]['resume']
                  and work[0]['status'] == 'completed' and work[0]['error_type'] is None)
    return {'coverage': 'complete_controller_intervals' if complete else (
                'partial_controller_intervals' if rows else 'unavailable'),
            'continuous_run_seconds': work[0]['wall_seconds'] if continuous else None,
            'controller_seconds': math.fsum(known) if complete else None,
            'known_controller_seconds': math.fsum(known) if rows else None,
            'unknown_intervals': len(work)-len(known), 'unmeasured_prefix': not complete_prefix,
            'replayed_invocations': sum(row['replayed_result'] is True for row in intervals),
            'intervals': intervals,
            'semantics': 'Measured inside the controller lease after manifest/constructor setup through journal recovery, method, cleanup and snapshot publication. Excludes lock wait, registry setup, process downtime and completed-result replays. Controller intervals are not background-worker active time. Only a completed uninterrupted initial invocation supplies continuous_run_seconds.'}
