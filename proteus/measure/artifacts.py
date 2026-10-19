"""Offline, codec-specific measurements of immutable artifacts and declared lineage.

No candidate or codec plugin is executed. Native surface instruments stay separate
from component/record counts; no cross-codec scalar or implicit DAG path is formed.
"""

import ast
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import tempfile
import sys
from typing import Literal

from proteus.artifacts import codecs
from proteus.artifacts.codecs import RecordsCodec, TextCodec, bytes_hash
from proteus.core.adapter import Surface
from proteus.core.contracts import ArtifactComponent, TEXT, require, unique
from proteus.core import serialization
from proteus.core.serialization import Contract, content_hash
from proteus.measure import distance


@dataclass(frozen=True, kw_only=True)
class MeasurementSurface(Contract):
    name: str = field(metadata=TEXT)
    subdir: str = field(metadata=TEXT)
    unit: Literal['file', 'directory', 'top_level_def'] = 'file'

    def _validate(self):
        require(bool(self.name.strip()), 'measurement surface name must be nonempty')
        path = PurePosixPath(self.subdir)
        require(not path.is_absolute() and '..' not in path.parts and str(path) == self.subdir
                and '\\' not in self.subdir and '\x00' not in self.subdir,
                'measurement surface must be a canonical relative artifact path')
        if self.subdir != '.':
            ArtifactComponent(path=self.subdir, content_hash=bytes_hash(b''), size_bytes=0)


@dataclass(frozen=True, kw_only=True)
class MeasurementSpec(Contract):
    file_surfaces: tuple[MeasurementSurface, ...] = ()
    lineage_paths: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def _validate(self):
        unique((surface.name for surface in self.file_surfaces), 'measurement surface')
        for name, path in self.lineage_paths.items():
            require(bool(name.strip()) and len(path) >= 2 and all(key.strip() for key in path),
                    'a named measurement path needs at least two candidate IDs')
            unique(path, 'measurement path candidate')


def _instrument():
    return {'name': 'proteus.artifact_units', 'version': 1,
            'python_version': sys.version,
            'implementation_hash': content_hash({name: bytes_hash(Path(module).read_bytes()) for name, module in (
                ('artifacts.py', __file__), ('distance.py', distance.__file__),
                ('codecs.py', codecs.__file__), ('serialization.py', serialization.__file__))})}


def _units(artifact, files, spec, instrument):
    codec = artifact.codec.to_dict()
    def result(surface, kind, units, reason=None, **details):
        definition = {'instrument': instrument, 'codec': codec, 'surface': surface, 'unit_kind': kind}
        return {'surface': surface['name'] if surface else None, 'unit_kind': kind,
                'metric_id': content_hash(definition) if kind else None,
                'status': 'measured' if units is not None else 'unsupported', 'reason': reason,
                'unit_count': len(units) if units is not None else None,
                'units_hash': content_hash(units) if units is not None else None,
                **details}, units

    if artifact.codec.version == '1' and artifact.codec.name == 'proteus.text_components':
        value = TextCodec().decode(files)
        return [result(None, 'text_component', {key: content_hash(text) for key, text in value.components.items()})]
    if artifact.codec.version == '1' and artifact.codec.name == 'proteus.structured_records':
        value = RecordsCodec().decode(files)
        return [result(None, 'structured_record', {key: content_hash(record) for key, record in value.records.items()})]
    if artifact.codec.version != '1' or artifact.codec.name != 'proteus.file_tree':
        return [result(None, None, None, 'unsupported_codec')]
    if not spec.file_surfaces:
        return [result(None, None, None, 'file_surfaces_not_declared')]
    measured = []
    for surface in spec.file_surfaces:
        selected = {name: item for name, item in files.items()
                    if (surface.subdir == '.' or name == surface.subdir or name.startswith(surface.subdir+'/'))
                    and '__pycache__' not in PurePosixPath(name).parts}
        definition = {'name': surface.name, 'subdir': surface.subdir, 'unit': surface.unit}
        kind = 'native_surface_'+surface.unit
        details = {'selected_files': len(selected), 'present': bool(selected), 'syntax_fallback_files': []}
        try:
            texts = {name: item.data.decode('utf-8') for name, item in selected.items()}
        except UnicodeDecodeError:
            measured.append(result(definition, kind, None, 'surface_is_not_utf8', **details))
            continue
        if surface.unit == 'top_level_def':
            if any(not name.endswith('.py') for name in selected):
                measured.append(result(definition, kind, None, 'surface_contains_non_python_files', **details))
                continue
            for name, text in texts.items():
                try:
                    ast.parse(text)
                except SyntaxError:
                    details['syntax_fallback_files'].append(name)
        # Only verified regular-file bytes enter the temporary tree, never links
        # or code execution. Existing native unit semantics are reused verbatim.
        with tempfile.TemporaryDirectory(prefix='proteus-measure-') as temporary:
            root = Path(temporary)
            for name, item in selected.items():
                path = root/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(item.data)
            units = distance.units(root, [Surface(surface.name, surface.subdir, surface.unit)])[surface.name]
        measured.append(result(definition, kind, units, **details))
    return measured


def _delta(before, after):
    row_a, units_a = before
    row_b, units_b = after
    empty = {key: None for key in ('added', 'dropped', 'revised', 'unchanged', 'union', 'distance', 'churn')}
    if units_a is None or units_b is None or row_a['metric_id'] != row_b['metric_id']:
        return {**empty, 'status': 'unsupported', 'reason': row_a['reason'] or row_b['reason'] or 'incompatible_units'}
    change = distance.delta(units_a, units_b, row_b['surface'])
    return {key: getattr(change, key) for key in empty} | {'status': 'measured', 'reason': None}


def measure_lineage(candidates, store, spec):
    """Return separately typed edge/path measurements from verified saved bytes."""
    require(type(spec) is MeasurementSpec, 'measurements require a MeasurementSpec')
    candidates = tuple(candidates)
    index = {candidate.id: candidate for candidate in candidates}
    require(len(index) == len(candidates), 'duplicate measurement candidate ID')
    require(len({candidate.run_id for candidate in candidates}) <= 1, 'measurement lineage spans runs')
    pending, visited = set(index), set()
    while pending:
        ready = {key for key in pending if set(index[key].parent_ids) <= visited}
        require(bool(ready), 'measurement lineage has a missing parent or cycle')
        pending -= ready
        visited |= ready
    for path in spec.lineage_paths.values():
        require(all(key in index for key in path), 'measurement path references a missing candidate')
        require(all(parent in index[child].parent_ids for parent, child in zip(path, path[1:])),
                'measurement path must follow recorded parent edges')
    instrument = _instrument()
    artifacts, inventories, cache = [], {}, {}
    for candidate in sorted(candidates, key=lambda item: item.id):
        artifact = candidate.artifact
        key = content_hash(artifact)
        if key not in cache:
            cache[key] = _units(artifact, store.files(artifact), spec, instrument)
        measured = cache[key]
        inventories[candidate.id] = {row['surface']: (row, units) for row, units in measured}
        for row, _ in measured:
            artifacts.append({'candidate_id': candidate.id, 'artifact_hash': artifact.content_hash,
                              'codec': artifact.codec.to_dict(), **row})
    edges = []
    edge_index = {}
    for child in sorted(candidates, key=lambda item: item.id):
        for parent_id in sorted(child.parent_ids):
            parent = index[parent_id]
            if parent.artifact.codec != child.artifact.codec:
                surfaces = {None}
            else:
                surfaces = inventories[child.id].keys()
            for surface in sorted(surfaces, key=lambda name: name or ''):
                before = inventories[parent_id].get(surface)
                after = inventories[child.id].get(surface)
                if parent.artifact.codec != child.artifact.codec:
                    unavailable = ({'metric_id': None, 'reason': 'different_codecs'}, None)
                    change = _delta(unavailable, unavailable)
                    metric_id, kind = None, None
                else:
                    change = _delta(before, after)
                    metric_id, kind = after[0]['metric_id'], after[0]['unit_kind']
                row = {'parent_id': parent_id, 'candidate_id': child.id,
                       'parent_artifact_hash': parent.artifact.content_hash, 'artifact_hash': child.artifact.content_hash,
                       'parent_codec': parent.artifact.codec.to_dict(), 'codec': child.artifact.codec.to_dict(),
                       'surface': surface, 'unit_kind': kind, 'metric_id': metric_id, **change}
                edges.append(row)
                edge_index.setdefault((parent_id, child.id), []).append(row)
    paths = []
    for name, ids in sorted(spec.lineage_paths.items()):
        steps = [edge_index[(parent, child)] for parent, child in zip(ids, ids[1:])]
        metrics = sorted({row['metric_id'] for step in steps for row in step}, key=lambda key: key or '')
        for metric_id in metrics:
            rows = [row for step in steps for row in step if row['metric_id'] == metric_id]
            complete = metric_id is not None and len(rows) == len(steps) and all(row['status'] == 'measured' for row in rows)
            paths.append({'path': name, 'candidate_ids': list(ids), 'metric_id': metric_id,
                          'surface': rows[0]['surface'], 'unit_kind': rows[0]['unit_kind'], 'edge_count': len(steps),
                          'status': 'measured' if complete else 'unsupported',
                          'reason': None if complete else 'path_has_unmeasured_or_incompatible_edges',
                          **{key: sum(row[key] for row in rows) if complete else None
                             for key in ('added', 'dropped', 'revised', 'unchanged')},
                          'edge_distance_sum': sum(row['distance'] for row in rows) if complete else None})
    return {'schema_version': 1, 'instrument': instrument, 'spec': spec.to_dict(), 'spec_hash': content_hash(spec),
            'artifacts': artifacts, 'edges': edges, 'paths': paths,
            'notes': ['Units are compared only under the same codec and metric identity; no cross-type total is defined.',
                      'Every recorded parent edge is reported; roots have no fabricated zero-distance parent.',
                      'Path sums require an explicit recorded parent chain; DAG chronology does not define a path.',
                      'These are content/unit changes, not semantic similarity, executable permissions or model quality.']}
