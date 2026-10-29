"""Explicit cross-run comparisons built from saved, individually audited evidence."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import statistics
from typing import Literal

from proteus.core.contracts import TEXT, require, unique
from proteus.core.serialization import Contract, content_hash
from proteus.recipes.export import report_run
from proteus.recipes.manifest import RunManifest


@dataclass(frozen=True, kw_only=True)
class StudyCase(Contract):
    condition: str = field(metadata=TEXT)
    run: str = field(metadata=TEXT)
    seed: int
    evaluation_ids: tuple[str, ...] = field(metadata={'minItems': 1})
    candidate_id: str | None = field(default=None, metadata=TEXT)

    def _validate(self):
        unique(self.evaluation_ids, 'study evaluation')


@dataclass(frozen=True, kw_only=True)
class StudySpec(Contract):
    title: str = field(metadata=TEXT)
    mode: Literal['reference_profiles', 'controlled']
    seeds: tuple[int, ...] = field(metadata={'minItems': 1})
    cases: tuple[StudyCase, ...] = field(metadata={'minItems': 1})
    aggregation: Literal['task_mean', 'cohort_mean'] = 'task_mean'
    varied_fields: tuple[str, ...] = ()
    baseline: str | None = field(default=None, metadata=TEXT)

    def _validate(self):
        unique(self.seeds, 'study seed')
        unique(((case.condition, case.seed) for case in self.cases), 'condition/seed pair')
        require(all(case.seed in self.seeds for case in self.cases), 'case uses an undeclared seed')
        conditions = {case.condition for case in self.cases}
        require(all({case.seed for case in self.cases if case.condition == condition} == set(self.seeds)
                    for condition in conditions), 'every condition needs every declared seed')
        require(self.baseline is None or self.baseline in conditions, 'baseline condition is missing')
        require(self.mode == 'controlled' or (not self.varied_fields and self.baseline is None),
                'reference profiles do not claim controlled differences or paired baseline deltas')
        unique(self.varied_fields, 'varied field')
        require(all(path.startswith('/') and path not in ('/', '/schema_version')
                    and '~' not in path for path in self.varied_fields), 'use non-root JSON pointer paths for variations')


def _stats(values):
    known = [value for value in values if value is not None]
    complete = len(known) == len(values)
    return {'n': len(values), 'known': len(known),
            'mean': statistics.mean(known) if complete else None,
            'sample_sd': statistics.stdev(known) if complete and len(known) > 1 else None}


def _normalize(recipe):
    result = deepcopy(recipe.to_dict())
    result.pop('name')
    result.pop('seed')
    if result['execution'] is not None:
        result['execution'].pop('seed')
    suites = [item['tasks'] for item in result['evaluations']]
    if result['tasks'] is not None:
        suites.append(result['tasks'])
    for suite in suites:
        suite.pop('seed', None)
        # File locations vary between generated studies; content identity remains
        # enforced by the suite's dataset hash and each actual evaluation batch.
        if suite['suite']['plugin']['name'] == 'proteus.json_tasks':
            suite['suite']['options']['path'] = '<pinned-dataset-location>'
    return result


def _mask(value, pointer):
    tokens = pointer[1:].split('/')
    parent = value
    for token in tokens[:-1]:
        if isinstance(parent, list):
            require(token.isdigit() and int(token) < len(parent), 'variation path is absent: '+pointer)
            parent = parent[int(token)]
        else:
            require(isinstance(parent, dict) and token in parent, 'variation path is absent: '+pointer)
            parent = parent[token]
    token = tokens[-1]
    if isinstance(parent, list):
        require(token.isdigit() and int(token) < len(parent), 'variation path is absent: '+pointer)
        old = parent[int(token)]
        parent[int(token)] = '<declared-variation>'
    else:
        require(isinstance(parent, dict) and token in parent, 'variation path is absent: '+pointer)
        old = parent[token]
        parent[token] = '<declared-variation>'
    return content_hash(old)


def compare_runs(spec, *, base='.', include_private=False):
    require(type(spec) is StudySpec, 'comparison requires a typed StudySpec')
    rows, manifests = [], {}
    randomizations = defaultdict(list)
    identities, protocols = set(), defaultdict(list)
    conditions = defaultdict(list)
    for case in spec.cases:
        root = (Path(base)/case.run).resolve()
        report = report_run(root, include_private=include_private)
        require(report['status'] == 'completed' and report['history_scope'] == 'cumulative_journal',
                'study comparison requires completed cumulative journal runs')
        identity = report['manifest_hash']
        require(identity not in identities, 'duplicate run manifest; a resume/copy is not another seed')
        identities.add(identity)
        manifest = RunManifest.from_json((root/'manifest.json').read_text())
        require(manifest.manifest_hash == identity and manifest.recipe.seed == case.seed,
                'case seed or run manifest differs')
        manifests[identity] = manifest
        # Seeds may differ from the outer replicate index, but each paired case
        # must use the same explicit mapping. Masking variations cannot erase it.
        randomization = {
            'recipe_seed': manifest.recipe.seed,
            'execution_seed': manifest.recipe.execution.seed,
            'evaluation_seeds': {item.id: item.tasks.seed for item in manifest.recipe.evaluations},
            'task_seed': manifest.recipe.tasks.seed if manifest.recipe.tasks is not None else None,
        }
        randomizations[case.seed].append(content_hash(randomization))
        selected = []
        target_protocol = []
        for evaluation_id in case.evaluation_ids:
            cohorts = [row for row in report['cohorts'] if row['evaluation_id'] == evaluation_id
                       and (case.candidate_id is None or row.get('candidate_id') == case.candidate_id)]
            require(len(cohorts) == 1 and cohorts[0]['status'] == 'complete',
                    'study evaluation must select exactly one complete candidate cohort: '+evaluation_id)
            cohort = cohorts[0]
            require(cohort['purpose'] == 'test', 'study quality must use an explicitly final test evaluation')
            tasks = [row for row in report['tasks'] if row['batch_id'] == cohort['batch_id']]
            evaluation = next(item for item in manifest.recipe.evaluations if item.id == evaluation_id)
            evaluator = next(item for item in manifest.lock.plugins if item.plugin == evaluation.evaluator.plugin)
            target_protocol.append({'dataset_hash': evaluation.tasks.dataset_hash,
                'dataset_version': evaluation.tasks.dataset_version, 'split': evaluation.tasks.split,
                'evaluator': evaluation.evaluator.to_dict(), 'evaluator_implementation': evaluator.implementation_hash,
                'tasks': [(row['task_id'], row['task_version'], row['occurrence']) for row in tasks],
                'state_policy': manifest.recipe.execution.state_policy.value,
                'feedback_timing': evaluation.feedback_timing})
            selected.append((cohort, tasks))
        values = [row['score'] for _, tasks in selected for row in tasks] if spec.aggregation == 'task_mean' else (
            [cohort['mean_score'] for cohort, _ in selected])
        quality = statistics.mean(values) if values and None not in values else None
        normalized = _normalize(manifest.recipe)
        condition_hash = content_hash(normalized)
        variation_hashes = {path: _mask(normalized, path) for path in spec.varied_fields}
        conditions[case.condition].append((condition_hash, manifest.lock_hash, normalized))
        protocols[case.seed].append(content_hash(target_protocol))
        accounting = report['accounting']
        actual = accounting['actual'] or {}
        row = {'condition': case.condition, 'seed': case.seed, 'run_id': report['run_id'],
            'manifest_hash': identity, 'report_hash': report['report_hash'], 'run': str(root),
            'method': report['method'], 'reference': report['reference'], 'quality': quality,
            'tasks': sum(len(tasks) for _, tasks in selected), 'aggregation': spec.aggregation,
            'selected_cohorts': [cohort['batch_id'] for cohort, _ in selected],
            'cost_usd': actual.get('cost_usd'), 'model_calls': actual.get('model_calls'),
            'evaluations': actual.get('evaluations'), 'summed_operation_seconds': actual.get('wall_seconds'),
            'ledger_span_seconds': accounting['ledger_span_seconds'],
            'continuous_run_seconds': report['timing']['continuous_run_seconds'],
            'controller_seconds': report['timing']['controller_seconds'],
            'timing_coverage': report['timing']['coverage'],
            'configuration_fingerprint': condition_hash, 'declared_variations': variation_hashes,
            'randomization': randomization}
        rows.append(row)
    for name, values in conditions.items():
        require(len({item[0] for item in values}) == 1, 'condition changes more than seeds or data locations: '+name)
        require(len({item[1] for item in values}) == 1, 'condition source/plugin/package lock changes across seeds: '+name)
    if spec.mode == 'controlled':
        require(all(len(set(values)) == 1 for values in randomizations.values()),
                'controlled paired cases use different execution/task seed mappings')
        require(all(len(set(values)) == 1 for values in protocols.values()),
                'controlled conditions use different final task/metric/state protocols')
        fingerprints = {content_hash(value[2]) for values in conditions.values() for value in values}
        require(len(fingerprints) == 1, 'undeclared recipe differences in controlled comparison')
        # Common code/SDK identity is required; method-specific plugins can differ,
        # but a shared plugin cannot silently change between conditions or seeds.
        sources, packages, plugins = set(), set(), {}
        for manifest in manifests.values():
            sources.add(content_hash(manifest.lock.sources))
            packages.add(content_hash(manifest.lock.packages))
            for plugin in manifest.lock.plugins:
                key = (plugin.plugin.name, plugin.plugin.version)
                require(key not in plugins or plugins[key] == plugin, 'shared plugin implementation differs')
                plugins[key] = plugin
        require(len(sources) == len(packages) == 1, 'controlled comparison uses different source/package pins')
    metrics = ('quality', 'cost_usd', 'model_calls', 'evaluations', 'summed_operation_seconds', 'ledger_span_seconds',
               'continuous_run_seconds', 'controller_seconds')
    summaries = []
    for condition in sorted(conditions):
        group = [row for row in rows if row['condition'] == condition]
        summaries.append({'condition': condition, 'reference': group[0]['reference'],
            'metrics': {key: _stats([row[key] for row in group]) for key in metrics}})
    deltas = []
    if spec.baseline is not None:
        baseline = {row['seed']: row for row in rows if row['condition'] == spec.baseline}
        for condition in sorted(conditions):
            if condition == spec.baseline:
                continue
            group = [row for row in rows if row['condition'] == condition]
            per_seed = [{'seed': row['seed'], 'quality_delta': None if row['quality'] is None or
                         baseline[row['seed']]['quality'] is None else row['quality']-baseline[row['seed']]['quality']}
                        for row in group]
            deltas.append({'condition': condition, 'baseline': spec.baseline, 'per_seed': per_seed,
                           'quality_delta': _stats([row['quality_delta'] for row in per_seed])})
    payload = {'schema_version': 1, 'study': spec.to_dict(), 'study_hash': content_hash(spec),
        'include_private': include_private, 'rows': rows, 'summaries': summaries, 'paired_deltas': deltas,
        'comparison_semantics': 'configurations match except declared variations, names and pinned JSON data locations; seed mappings match within every paired replicate'
            if spec.mode == 'controlled' else 'descriptive reference profiles; no matched-condition or baseline-delta claim',
        'notes': ['Quality aggregates only the explicitly selected final-test cohorts.',
                  'Incomplete/hidden scores and unknown costs keep the whole aggregate unavailable.',
                  'Ledger event span includes pauses; summed operation time can overlap.',
                  'Continuous run time requires an uninterrupted initial controller invocation. Missing intervals keep total controller time unknown; background-worker activity is not measured by controller time.',
                  'Reference status and deviations are retained, not promoted by this report.']}
    return {**payload, 'comparison_hash': content_hash(payload)}


def summarize_study(path, destination, *, include_private=False):
    path, destination = Path(path).resolve(), Path(destination).resolve()
    spec = StudySpec.from_json(path.read_text())
    roots = [(path.parent/case.run).resolve() for case in spec.cases]
    require(all(not destination.is_relative_to(root) for root in roots), 'comparison must be written outside every run')
    report = compare_runs(spec, base=path.parent, include_private=include_private)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)+'\n')
    return destination
