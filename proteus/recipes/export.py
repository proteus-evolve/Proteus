"""Read saved research evidence without importing or executing method plugins.

This is an integrity/aggregation report, not an independent regrading of arbitrary
metrics. Domain reports may additionally regrade their pinned task references.
"""

from collections import Counter, defaultdict
import csv
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from proteus.artifacts.codecs import bytes_hash
from proteus.artifacts.store import ArtifactStore, CandidateEntry, _read
from proteus.core.contracts import (
    Candidate, DataRef, EvaluationBatch, OperationStatus, Rollout, StageRequest, StageResult, require,
)
from proteus.core.ledger import Amounts, DIMENSIONS, Ledger, TERMINAL
from proteus.core.methods import MethodResult
from proteus.core.run_lock import run_lock
from proteus.core.serialization import content_hash, read_json
from proteus.evaluation.records import EvaluationReceipt, Grade, TrialRecord
from proteus.evaluation.tasks import TaskBatch
from proteus.recipes.config import EvaluationSpec
from proteus.recipes.manifest import RunManifest
from proteus.recipes.controller_timing import report_timing


def _total(rows, key):
    values = [row[key] for row in rows]
    return None if None in values else float(sum((Decimal(str(value)) for value in values), Decimal(0)))


class SavedRun:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.evidence = {}
        self.manifest = RunManifest.from_dict(self.read('manifest.json'))
        self.recipe = self.manifest.recipe
        self.result = MethodResult.from_dict(self.read('result.json'))
        paths = sorted(self.root.glob('execution-*.json'))
        require(bool(paths), 'run has no saved execution snapshot')
        self.ledger_entries = {}
        self.snapshot = paths[-1].name
        self.records = self.read(self.snapshot)
        require(self.records['manifest_hash'] == self.manifest.manifest_hash, 'execution manifest differs')
        require(content_hash(self.records['values']) == self.records['values_hash'], 'execution values hash differs')
        self.values = {}
        for record in self.records['values']:
            ref = DataRef.from_dict(record['ref'])
            require(ref.content_hash == content_hash(record['value'])
                    and ref.id == content_hash([ref.kind, ref.content_hash]), 'saved value identity differs')
            require(ref.id not in self.values, 'duplicate saved value')
            self.values[ref.id] = record
        for ref in self.result.outputs.values():
            self.value(ref)
        self.candidates = self.index('candidates', Candidate)
        self.rollouts = self.index('rollouts', Rollout)
        self._artifacts()

    def read(self, relative):
        path = self.root / relative
        require(path.resolve().is_relative_to(self.root), 'evidence path escapes run')
        data = _read(path)
        self.evidence[str(relative)] = bytes_hash(data)
        return read_json(data.decode('utf-8'))

    def index(self, key, schema):
        objects = [schema.from_dict(item) for item in self.records[key]]
        require(all(item.run_id == self.manifest.run_id for item in objects), 'evidence run identity differs')
        require(len({item.id for item in objects}) == len(objects), 'duplicate evidence identity')
        return {item.id: item for item in objects}

    def value(self, ref):
        require(ref.id in self.values and self.values[ref.id]['ref'] == ref.to_dict(), 'missing or changed value')
        return self.values[ref.id]['value']

    def _artifacts(self):
        # files() checks manifest and blob identity without calling a codec.
        store = ArtifactStore(self.root/'artifacts', registry=None)
        visited = set()
        pending = set(self.candidates)
        while pending:
            ready = [key for key in sorted(pending) if set(self.candidates[key].parent_ids) <= visited]
            require(bool(ready), 'candidate lineage has a missing parent or cycle')
            for key in ready:
                candidate = self.candidates[key]
                digest = content_hash([candidate.run_id, key]).split(':')[1]
                ref = DataRef.from_dict(self.read(f'artifacts/candidates/{digest}.json'))
                require(ref.kind == 'proteus.candidate_entry@1' and ref.id == 'sha256:'+digest,
                        'candidate store index differs')
                entry = CandidateEntry.from_dict(self.read(
                    'artifacts/candidate-entries/'+ref.content_hash.split(':')[1]+'.json'))
                require(content_hash(entry) == ref.content_hash and entry.candidate == candidate,
                        'candidate snapshot differs from store')
                store.files(candidate.artifact)
                self.read('artifacts/manifests/'+candidate.artifact.content_hash.split(':')[1]+'.json')
                for component in candidate.artifact.components:
                    self.evidence['artifacts/blobs/'+component.content_hash.split(':')[1]] = component.content_hash
                visited.add(key)
                pending.remove(key)

    def evaluations(self, include_private):
        rows, cohorts, covered = [], [], set()
        public_batches = {item['id']: EvaluationBatch.from_dict(item) for item in self.records['evaluations']}
        require(len(public_batches) == len(self.records['evaluations']), 'duplicate observed batch')
        observed = set()
        specs = {item.id: item for item in self.recipe.evaluations}
        for path in sorted((self.root/'evaluations').glob('*/protocol.json')):
            prefix = path.parent.relative_to(self.root).as_posix()
            protocol = self.read(prefix+'/protocol.json')
            spec = EvaluationSpec.from_dict(protocol['evaluation'])
            batch = TaskBatch.from_dict(protocol['batch'])
            require(spec.id in specs and spec == specs[spec.id], 'evaluation recipe differs')
            require(protocol['execution'] == self.recipe.execution.to_dict(), 'evaluation execution differs')
            require(batch.dataset_hash == spec.tasks.dataset_hash and batch.dataset_version == spec.tasks.dataset_version
                    and batch.split == spec.tasks.split and batch.suite == spec.tasks.suite.plugin,
                    'evaluation task source differs')
            require(protocol['reserved_evaluations'] == len(batch.tasks), 'evaluation reservation differs')
            inputs = {key: DataRef.from_dict(value) for key, value in protocol['inputs'].items()}
            for ref in inputs.values():
                self.value(ref)
            protocol_hash = content_hash({'spec': spec, 'batch': batch, 'subject': self.recipe.subject,
                'execution': self.recipe.execution, 'lock_hash': self.manifest.lock_hash, 'inputs': inputs})
            receipt_path = self.root/prefix/'receipt.json'
            if not receipt_path.exists():
                require(self.result.status != OperationStatus.COMPLETED, 'completed run has an incomplete evaluation')
                cohorts.append({'evaluation_id': spec.id, 'status': 'incomplete', 'protocol_hash': protocol_hash,
                                'evidence': prefix+'/protocol.json', 'mean_score': None})
                continue
            receipt = EvaluationReceipt.from_dict(self.read(prefix+'/receipt.json'))
            require(path.parent.name == content_hash(receipt.id).split(':')[1]
                    and receipt.task_batch_hash == content_hash(batch), 'receipt protocol identity differs')
            receipt_hash = content_hash(receipt)
            receipt_ref = DataRef(kind='proteus.eval.receipt@1',
                id=content_hash(['proteus.eval.receipt@1', receipt_hash]), content_hash=receipt_hash)
            require(self.value(receipt_ref) == receipt.to_dict(), 'receipt differs from committed execution value')
            require(len(receipt.rollout_ids) == len(batch.tasks) and len(set(receipt.rollout_ids)) == len(batch.tasks),
                    'receipt task coverage differs')
            private = [EvaluationBatch.from_dict(self.read(p.relative_to(self.root).as_posix()))
                       for p in sorted((path.parent/'private/batches').glob('*.json'))]
            require(bool(private), 'completed evaluation has no grade batches')
            by_rollout = {}
            for grades in private:
                require(grades.run_id == self.manifest.run_id and grades.operation_id == receipt.id
                        and grades.candidate_id in self.candidates and grades.protocol_hash == protocol_hash
                        and grades.evaluator == spec.evaluator.plugin and grades.visibility == spec.visibility
                        and grades.artifact_hash == self.candidates[grades.candidate_id].artifact.content_hash,
                        'grade batch identity differs')
                if spec.visibility.value == 'observed':
                    require(public_batches.get(grades.id) == grades, 'observed grades differ from saved snapshot')
                    observed.add(grades.id)
                for row in grades.rows:
                    require(row.rollout_id not in by_rollout, 'duplicate graded rollout')
                    by_rollout[row.rollout_id] = (grades, row)
            require(set(by_rollout) == set(receipt.rollout_ids), 'grade batch coverage differs')
            require(receipt.batches == (tuple(private) if spec.visibility.value == 'observed' else ()),
                    'receipt feedback visibility differs')
            visible = include_private or spec.visibility.value == 'observed'
            for index, (task, rollout_id, candidate_id) in enumerate(zip(batch.tasks, receipt.rollout_ids, receipt.candidate_ids)):
                require(rollout_id not in covered and rollout_id in self.rollouts, 'missing or duplicate rollout')
                rollout = self.rollouts[rollout_id]
                grades, row = by_rollout[rollout_id]
                require(candidate_id in self.candidates and candidate_id == grades.candidate_id
                        and rollout.candidate_id == candidate_id and rollout.artifact_hash == grades.artifact_hash
                        and rollout.operation_id == receipt.id and rollout.attempt_id == grades.attempt_id
                        and rollout.task_id == row.task_id == task.id and rollout.task_version == task.version
                        and rollout.split == task.split, 'task/grade/candidate binding differs')
                expected_execution = content_hash({'execution': self.recipe.execution, 'inputs': inputs}) if inputs else (
                    content_hash(self.recipe.execution))
                require(rollout.execution_hash == expected_execution, 'rollout execution differs')
                grade_record = self.read(prefix+f'/private/grades/{index:06d}.json')
                grade = Grade.from_dict(grade_record['grade'])
                require(grade_record['row'] == row.to_dict()
                        and (grade.status, grade.score, dict(grade.metrics)) == (row.status, row.score, dict(row.metrics)),
                        'grade row differs from original grading record')
                grader_id = 'grader-'+content_hash([receipt.id, rollout.attempt_id, index]).split(':')[1]
                if grader_id in self.ledger_entries:
                    entry = self.ledger_entries[grader_id]
                    require(entry['settlement']['result']['grade'] == grade.to_dict(), 'grade differs from ledger settlement')
                prediction = prefix+f'/predictions/{index:06d}.json'
                trial = TrialRecord.from_dict(self.read(prediction))
                require(trial.rollout == rollout and trial.task_hash == content_hash(task) and trial.inputs == inputs,
                        'saved prediction differs from snapshot')
                for ref in (rollout.state_before, rollout.state_after, *rollout.trace):
                    if ref is not None:
                        self.value(ref)
                rows.append({'evaluation_id': spec.id, 'purpose': spec.purpose, 'visibility': spec.visibility.value,
                    'operation_id': receipt.id, 'batch_id': grades.id, 'protocol_hash': protocol_hash,
                    'candidate_id': candidate_id, 'artifact_hash': rollout.artifact_hash, 'rollout_id': rollout_id,
                    'task_id': task.id, 'task_version': task.version, 'split': task.split, 'occurrence': index,
                    'rollout_status': rollout.status.value, 'grade_status': row.status.value if visible else None,
                    'score': row.score if visible else None, 'metrics': dict(row.metrics) if visible else {},
                    'score_availability': ('recorded' if row.score is not None else 'unavailable') if visible else 'hidden',
                    'prediction': prediction})
                covered.add(rollout_id)
            for grades in private:
                cohorts.append({'evaluation_id': spec.id, 'purpose': spec.purpose, 'visibility': spec.visibility.value,
                    'operation_id': receipt.id, 'batch_id': grades.id, 'protocol_hash': protocol_hash,
                    'candidate_id': grades.candidate_id, 'artifact_hash': grades.artifact_hash,
                    'status': 'complete', 'tasks': len(grades.rows),
                    'scored_tasks': sum(row.score is not None for row in grades.rows) if visible else None,
                    'mean_score': grades.mean_score() if visible else None,
                    'score_availability': ('recorded' if grades.mean_score() is not None else 'unavailable') if visible else 'hidden',
                    'evidence': prefix+'/receipt.json'})
        require(observed == set(public_batches), 'unreported observed batches')
        if self.result.status == OperationStatus.COMPLETED:
            require(covered == set(self.rollouts), 'completed run has unreported rollouts')
        return rows, cohorts, sorted(set(self.rollouts)-covered)

    def accounting(self):
        path = self.root/'private/ledger.sqlite3'
        if not path.exists():
            require(self.records.get('ledger') is None, 'recorded ledger is missing')
            return {'coverage': 'unavailable', 'actual': None, 'entries': [], 'by_role_stage': [],
                    'ledger_span_seconds': None}
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            require(db.execute('PRAGMA user_version').fetchone()[0] == 1, 'unsupported ledger schema')
            identities = db.execute('SELECT value FROM identity').fetchall()
            require(len(identities) == 1 and read_json(identities[0][0]) == {'schema_version': 1,
                'run_id': self.manifest.run_id, 'config_hash': self.manifest.config_hash,
                'lock_hash': self.manifest.lock_hash}, 'ledger identity differs')
            entries = [Ledger._row(row) for row in db.execute('SELECT * FROM entries ORDER BY id')]
            self.ledger_entries = {entry['id']: entry for entry in entries}
            charged, unknown, active, count = Ledger._balance(db, 'run')
            require(count == len(entries), 'ledger contains operations outside the run budget')
            require(active == 0 and all(entry['state'] in TERMINAL for entry in entries),
                    'export requires settled operations; reconcile or finish recovery first')
            events = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY sequence')]
            grouped = defaultdict(list)
            for event in events:
                event['payload_json'] = read_json(event['payload_json'])
                grouped[event['entry']].append(event)
            rows = []
            for entry in entries:
                actual = Amounts.from_dict(entry['settlement']['actual']).to_dict()
                history = grouped[entry['id']]
                require(history and history[0]['kind'] == 'reserved'
                        and history[0]['payload_json'] == entry['identity']
                        and history[-1]['kind'] == 'settled'
                        and history[-1]['payload_json'] == entry['settlement']
                        and entry['settlement']['status'] == entry['state'], 'ledger event/settlement differs')
                metadata = entry['identity']['metadata']
                rows.append({'entry_id': entry['id'], 'kind': metadata.get('kind'), 'role': metadata.get('role'),
                    'stage': metadata.get('stage'), 'operation_id': metadata.get('operation_id'),
                    'attempt_id': metadata.get('attempt_id'), 'worker_id': metadata.get('worker_id'),
                    'status': entry['state'], **{key: actual[key] for key in DIMENSIONS},
                    'violations': entry['settlement']['violations']})
            ledger_hash = content_hash({'entries': entries, 'events': events})
        self.evidence['private/ledger.sqlite3#logical-snapshot'] = ledger_hash
        groups = defaultdict(list)
        for row in rows:
            groups[(row['role'], row['stage'], row['kind'])].append(row)
        aggregates = [{'role': key[0], 'stage': key[1], 'kind': key[2], 'entries': len(values),
                       **{dimension: _total(values, dimension) for dimension in DIMENSIONS}}
                      for key, values in sorted(groups.items(), key=lambda pair: str(pair[0]))]
        return {'coverage': 'managed ledger operations; opaque native calls may be outside this ledger',
            'actual': {key: _total(rows, key) for key in DIMENSIONS},
            'budget_charged': {key: None if value is None else float(value) for key, value in charged.items()},
            'unknown_actual_entries': unknown, 'entries': rows, 'by_role_stage': aggregates,
            'ledger_span_seconds': (max(event['time_ns'] for event in events)-min(event['time_ns'] for event in events))/1e9
                if events else None,
            'time_semantics': 'wall_seconds is summed operation time; ledger_span_seconds includes gaps/recovery downtime and is not an active-runtime measurement'}

    def native_accounting(self):
        from proteus.core.native_inputs import input_directory, read_native_inputs
        from proteus.core.native_usage import summarize_native_inputs
        native_root = self.root/'workspace'
        records = read_native_inputs(native_root)
        require(records == self.records.get('native_inputs', []), 'native input snapshot differs from receipts')
        current = {row['input']['id']: row for row in records}
        # Cumulative snapshots must not erase completed receipts or interrupted
        # inputs from earlier invocations. A repeated snapshot is not a new call.
        for path in sorted(self.root.glob('execution-*.json')):
            prior = self.read(path.name)
            require(prior['manifest_hash'] == self.manifest.manifest_hash, 'native history manifest differs')
            for row in prior.get('native_inputs', []):
                saved = current.get(row['input']['id'])
                require(saved is not None and saved['input'] == row['input']
                        and (row['outcome'] is None or saved['outcome'] == row['outcome']),
                        'native input history is missing or changed')
        directory = input_directory(native_root)
        for row in records:
            for suffix in ('input', 'outcome'):
                if row[suffix] is not None:
                    self.read((directory/(row['input']['id']+'.'+suffix+'.json')).relative_to(self.root).as_posix())
        return summarize_native_inputs(records)

    def stages(self):
        requests, rows = {}, []
        for raw in self.records['requests']:
            request = StageRequest.from_dict(raw)
            key = (request.operation_id, request.attempt_id)
            require(request.run_id == self.manifest.run_id and key not in requests, 'duplicate/foreign stage request')
            requests[key] = request
        seen = set()
        for raw in self.records['results']:
            result = StageResult.from_dict(raw)
            key = (result.operation_id, result.attempt_id)
            require(key in requests and key not in seen and result.run_id == self.manifest.run_id,
                    'stage result binding differs')
            seen.add(key)
            request = requests[key]
            rows.append({'operation_id': key[0], 'attempt_id': key[1], 'stage': request.stage.name,
                         'role': request.role, 'status': result.status.value})
        counts = Counter(row['operation_id'] for row in rows)
        return rows, {'statuses': dict(Counter(row['status'] for row in rows)),
                      'recorded_attempts': len(rows), 'operations_with_multiple_attempts': sum(n > 1 for n in counts.values()),
                      'additional_attempts': sum(n-1 for n in counts.values()), 'unfinished_requests': len(requests)-len(seen)}


def report_run(root, *, include_private=False):
    """Export a quiescent run; no plugins, model SDKs or task graders are executed."""
    root = Path(root).resolve()
    require(root.is_dir(), 'run directory is missing')
    with run_lock(root):
        run = SavedRun(root)
        accounting = run.accounting()
        native_accounting = run.native_accounting()
        timing = report_timing(run)
        tasks, cohorts, unreported = run.evaluations(include_private)
        stages, attempts = run.stages()
        candidates = [{'candidate_id': item.id, 'artifact_hash': item.artifact.content_hash,
            'codec': item.artifact.codec.to_dict(), 'parent_ids': list(item.parent_ids), 'created_by': item.created_by,
            'created_attempt_id': item.created_attempt_id, 'validation': item.validation.value}
            for item in run.candidates.values()]
        report = {'schema_version': 1, 'report_kind': 'saved-recipe-evidence', 'run_id': run.manifest.run_id,
            'manifest_hash': run.manifest.manifest_hash, 'config_hash': run.manifest.config_hash,
            'lock_hash': run.manifest.lock_hash, 'method': run.recipe.method.plugin.to_dict(),
            'reference': run.recipe.reference.to_dict(), 'execution_seed': run.recipe.execution.seed if run.recipe.execution else None,
            'status': run.result.status.value, 'snapshot': run.snapshot, 'include_private': include_private,
            'history_scope': 'cumulative_journal' if any(item.plugin == run.recipe.method.plugin
                and 'run.journal' in item.capabilities for item in run.manifest.lock.plugins) else 'latest_invocation_only',
            'verification': 'saved evidence identities and aggregation; evaluator semantics are not re-executed',
            'source_run': str(root),
            'candidates': candidates, 'tasks': tasks, 'cohorts': cohorts, 'accounting': accounting, 'timing': timing,
            'native_accounting': native_accounting,
            'stages': stages, 'attempts': attempts, 'unreported_rollout_ids': unreported,
            'evidence': {path: digest for path, digest in run.evidence.items()
                         if include_private or not (path.startswith(('private/', '.proteus-records/')) or '/private/' in path)},
            'notes': ['Only the latest execution snapshot is used; journal methods restore cumulative history, while other methods cover the latest invocation. Resume snapshots are not additional trials.',
                      'Unknown actual usage stays null even when the budget ledger charged a reservation.',
                      'Native boundary usage is reported separately, without ledger enforcement or complete internal-call coverage.',
                      'Private grades require include_private; raw prompts, predictions and task references are not copied.']}
        return {**report, 'report_hash': content_hash(report)}


def export_run(root, destination, *, include_private=False):
    destination = Path(destination).resolve()
    root = Path(root).resolve()
    require(not destination.is_relative_to(root), 'export destination must be outside the run')
    require(not destination.exists(), 'export destination already exists')
    report = report_run(root, include_private=include_private)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.proteus-export-', dir=destination.parent))
    try:
        (temporary/'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)+'\n')
        tables = {'tasks': report['tasks'], 'cohorts': report['cohorts'], 'candidates': report['candidates'],
                  'costs': report['accounting']['entries'], 'role-stage-costs': report['accounting']['by_role_stage'],
                  'stage-attempts': report['stages'], 'controller-intervals': report['timing']['intervals'],
                  'native-usage': report['native_accounting']['entries']}
        for name, rows in tables.items():
            with (temporary/(name+'.csv')).open('w', encoding='utf-8', newline='') as stream:
                if not rows:
                    continue
                writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(key for row in rows for key in row)))
                writer.writeheader()
                writer.writerows({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else
                                  ('null' if value is None else value) for key, value in row.items()} for row in rows)
        # mkdir is exclusive: a racing destination cannot be overwritten.
        destination.mkdir()
        for path in temporary.iterdir():
            path.replace(destination/path.name)
    finally:
        shutil.rmtree(temporary)
    return destination/'report.json'
