"""Compact, scoped feedback from ALREADY RELEASED gateway bytes only.

Pure functions: no filesystem, private reports, networking, or candidate execution.
Candidate output and model-authored notes never establish measured fitness.
"""
import base64
import hashlib
import json
import math

SCORED = {'succeeded', 'task_failed'}
ALLOWED_EVALUATIONS = {'packing'}
ALLOWED_SPLITS = {'optimization'}
MAX_PROMPT_CHARS = 96000
MAX_RECENT_FEEDBACK_CHARS = 24000
START = '\nMEASURED_RECENT_FEEDBACK_JSON\n'
END = '\nEND_MEASURED_RECENT_FEEDBACK_JSON\n'


class FeedbackIntegrityError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise FeedbackIntegrityError(reason)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def text(value, maximum=1000):
    if value is None:
        return ''
    value = str(value)
    return value if len(value) <= maximum else value[:maximum] + ' [diagnostic excerpt; full released payload retained]'


def files_by_path(files):
    result = {}
    for item in files:
        require(item['path'] not in result, 'Duplicate released file path')
        result[item['path']] = item
    return result


def decoded(item):
    return base64.b64decode(item['base64'], validate=True)


def released_records(view, files, *, identity=None, candidate=None, operation=None):
    """Manifest-first lookup; reject wrong scope, corruption and hidden batches."""
    access = view['access']
    require(set(access['evaluation_ids']) <= ALLOWED_EVALUATIONS
            and set(access['splits']) <= ALLOWED_SPLITS, 'Unexpected/hidden evidence scope')
    if identity:
        require(all(view.get(k) == identity.get(k) for k in ('run_id', 'config_hash', 'lock_hash')),
                'Released evidence belongs to a different run/config/lock')
    require(len(view['candidate_ids']) == 1, 'Expected one submitted candidate')
    candidate = candidate or view['candidate_ids'][0]
    require(view['candidate_ids'] == [candidate], 'Released candidate binding differs')
    supplied = files_by_path(files)
    result = []
    for entry in view['entries']:
        if entry['kind'] not in {'feedback', 'evaluation', 'rollout', 'trace'}:
            continue  # Never decode candidate source, RESEARCH.md or task documents.
        require(entry['evaluation_id'] in ALLOWED_EVALUATIONS and entry['split'] in ALLOWED_SPLITS
                and entry['candidate_id'] == candidate, 'Released entry exceeds target scope')
        if operation is not None:
            require(entry['operation_id'] == operation, 'Feedback belongs to another submission')
        require(entry['path'] in supplied, 'Released feedback file missing')
        raw = decoded(supplied[entry['path']])
        require(len(raw) == entry['size_bytes'] and digest(raw) == entry['content_hash'],
                'Released feedback bytes differ from evidence manifest')
        value = json.loads(raw)
        if entry['kind'] == 'evaluation':
            require(value.get('visibility') == 'observed', 'Private evaluation cannot enter a prompt')
        result.append((entry, value))
    return candidate, result


def diagnostic_fields(value, path='$', depth=0):
    """Only diagnostic/runtime fields; ignore outputs and all claimed scores."""
    if depth > 8:
        return []
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {'output', 'code', 'base64', 'loaded', 'state', 'artifact', 'components'}:
                continue
            if key in {'diagnostic', 'details', 'error', 'exception_type', 'exception_message', 'stderr'} and child:
                found.append({'field': path + '.' + key, 'text': text(child)})
            elif key in {'return_code', 'elapsed_seconds', 'wall_seconds', 'timeout_seconds', 'deadline_exceeded'}:
                if child is not None and type(child) in (int, float, bool):
                    found.append({'field': path + '.' + key, 'value': child})
            elif key in {'value', 'events', 'execution', 'usage', 'trace', 'exception_info'}:
                found.extend(diagnostic_fields(child, path + '.' + key, depth + 1))
    elif isinstance(value, list):
        for index, child in enumerate(value[:8]):
            found.extend(diagnostic_fields(child, f'{path}[{index}]', depth + 1))
    return found


def summarize_view(view, files, *, identity=None, candidate=None, operation=None,
                   ticket_id=None, worker_id=None, interaction_status='completed', gateway_diagnostic=None):
    candidate, records = released_records(view, files, identity=identity, candidate=candidate, operation=operation)
    rows, predictions, diagnostics, used = {}, {}, [], []
    for entry, value in records:
        used.append({'path': entry['path'], 'sha256': entry['content_hash']})
        if entry['kind'] == 'evaluation':
            values = value['rows']
        elif entry['kind'] == 'feedback' and entry['path'].endswith('/grade.json'):
            values = [value]
        else:
            values = []
        for row in values:
            rid = row['rollout_id']
            score, status = row.get('score'), row['status']
            require((score is not None) == (status in SCORED), 'Unavailable outcome cannot have an invented score')
            require(score is None or (type(score) in (int, float) and math.isfinite(score)), 'Nonfinite/invalid score')
            compact = {'rollout_id': rid, 'task_id': row['task_id'], 'outcome_status': status,
                       'score': score, 'diagnostic': text(row.get('diagnostic'))}
            require(rid not in rows or rows[rid] == compact, 'Released batch/grade outcomes disagree')
            rows[rid] = compact
        if entry['kind'] == 'rollout':
            predictions[value['id']] = {'status': value['status'],
                'diagnostic': text(value.get('diagnostic')),
                'wall_seconds': (value.get('usage') or {}).get('wall_seconds')}
        diagnostics.extend(diagnostic_fields(value))
    require(len(rows) <= 4, 'Packing prompt expects a small explicit outcome set; refuse lossy truncation')
    for rid, row in rows.items():
        prediction = predictions.get(rid)
        if prediction:
            row['prediction_status'] = prediction['status']
            row['wall_seconds'] = prediction['wall_seconds']
    available = bool(rows) and all(row['score'] is not None for row in rows.values())
    score = math.fsum(row['score'] / len(rows) for row in rows.values()) if available else None
    statuses = list(dict.fromkeys(row['outcome_status'] for row in rows.values()))
    unique_diagnostics = []
    for item in diagnostics:
        if item not in unique_diagnostics:
            unique_diagnostics.append(item)
    return {'schema_version': 3, 'ticket_id': ticket_id, 'worker_id': worker_id,
        'candidate_id': candidate, 'interaction_status': interaction_status,
        'gateway_diagnostic': text(gateway_diagnostic),
        'outcome_status': statuses[0] if len(statuses) == 1 else ('mixed' if statuses else 'unavailable'),
        'score': score, 'fitness_available': available,
        'missingness': None if available else ('No authorized grade rows were released' if not rows else
            'Score explicitly unavailable; statuses: ' + ', '.join(statuses)),
        'outcomes': list(rows.values()), 'runtime_and_diagnostics': unique_diagnostics[:8],
        'diagnostic_entries_omitted': max(0, len(unique_diagnostics) - 8),
        'evidence_files': used[:8], 'evidence_file_entries_omitted': max(0, len(used) - 8),
        'provenance': 'Authenticated gateway released view/files only; no private filesystem/report access',
        'model_claims_used_as_fitness': False}


def summarize_gateway(payload, *, ticket_id, identity=None):
    result, ticket = payload.get('result') or {}, payload['ticket']
    require(ticket['evaluation_id'] in ALLOWED_EVALUATIONS, 'Unexpected evaluation ticket')
    if not payload.get('view'):
        return {'schema_version': 3, 'ticket_id': ticket_id, 'worker_id': ticket['worker_id'],
            'candidate_id': (result.get('candidate') or {}).get('id'),
            'interaction_status': payload['status'], 'outcome_status': 'unavailable',
            'score': None, 'fitness_available': False, 'outcomes': [],
            'missingness': 'Gateway returned no authorized grade view',
            'gateway_diagnostic': text(result.get('diagnostic')), 'model_claims_used_as_fitness': False}
    view = payload['view']
    evidence = result.get('evidence') or {}
    require(evidence.get('content_hash') == digest(canonical(view).encode()), 'Released view reference hash mismatch')
    return summarize_view(view, payload['files'], identity=identity,
        candidate=result['candidate']['id'], operation='gateway:' + ticket_id + ':evaluation',
        ticket_id=ticket_id, worker_id=ticket['worker_id'], interaction_status=payload['status'],
        gateway_diagnostic=result.get('diagnostic'))


def summarize_knowledge(record, *, identity=None):
    write = record['revision']['write']
    require(record['ref']['content_hash'] == digest(canonical(record['revision']).encode()),
            'Shared revision reference hash mismatch')
    if write['kind'] == 'attempts':
        exported = files_by_path(record['files'])
        require('view.json' in exported, 'Shared formal attempt lacks its authorized view')
        raw_view = decoded(exported['view.json'])
        component = next((c for c in write['artifact']['components'] if c['path'] == 'view.json'), None)
        require(component is not None and component['content_hash'] == digest(raw_view)
                and component['size_bytes'] == len(raw_view), 'Shared view bytes differ from revision artifact')
        view = json.loads(raw_view)
        files = [{**item, 'path': path[len('evidence/'):]} for path, item in exported.items()
                 if path.startswith('evidence/')]
        return {'source': record['ref'], 'kind': 'measured_shared_attempt',
            'feedback': summarize_view(view, files, identity=identity, ticket_id=write['key'], worker_id=write['writer_id'])}
    return {'source': record['ref'], 'kind': 'model_authored_notes_or_skills',
        'verified_fitness': False, 'author': write['writer_id'],
        'text': '\n'.join(decoded(f).decode(errors='replace')[:1600] for f in record['files'][:3])[:4000]}


def render_prompt(*, orientation, worker, proposal, maximum, source, recent, shared):
    recent = list(recent)[:5]  # Caller supplies newest first.
    feedback = canonical({'order': 'latest_first', 'records': recent})
    require(len(feedback) <= MAX_RECENT_FEEDBACK_CHARS, 'Recent measured feedback cannot fit losslessly; no API dispatch')
    instructions = (
        'Write a complete executable Python module to replace initial_program.py and improve its circle packing. '
        'Return exactly one JSON object with two string fields named code and note, and no markdown or other text. '
        'The code string MUST contain the full actual Python source with escaped newlines, imports and all function bodies. '
        'It must NOT be a filename, an instruction, a description, a schema example, placeholder text, ellipsis or TODO. '
        'The note string must describe your actual algorithmic change or finding, not repeat a formatting label. '
        'Define an actual module-level run() function returning centers, radii, sum_radii. '
        'You may implement packing directly in run() or use helpers such as construct_packing(); '
        'construct_packing is not a required external API. Include every helper your module needs. '
        'Pack exactly26circles in the unit square to maximize verified sum of radii: centers shape(26,2), radii shape(26,). '
        'Use only stdlib/numpy/scipy; no network/shell/modelAPI/fileIO. Bound computation within600seconds. '
        'Any schema field names are formatting instructions, never literal answer values. Read CURRENT_SOURCE below '
        'and return a concrete executable revision of that complete module. '
        'The measured feedback below is authoritative. interaction_status=completed means ONLY that the gateway finished; '
        'read outcome_status, fitness_available, nullable score and diagnostics. Never turn missing/timeout/infra into0. '
        'Model-authored notes/claims are NOT measured fitness. Cite exact shared sources you use.\n'
        f'Worker {worker}; proposal {proposal}/{maximum}.\n'
    )
    mandatory = instructions + START + feedback + END + '\nCONTROLLER ORIENTATION\n' + orientation[:12000]
    require(len(mandatory) < MAX_PROMPT_CHARS, 'Mandatory measured feedback must remain intact')
    optional_budget = min(10000, MAX_PROMPT_CHARS - len(mandatory))
    chosen = []
    for item in shared:
        if len(canonical(chosen + [item])) > optional_budget:
            continue
        chosen.append(item)
    prompt = mandatory + '\nAUTHORIZED_SHARED_KNOWLEDGE_JSON\n' + canonical(chosen)
    remaining = MAX_PROMPT_CHARS - len(prompt) - len('\nCURRENT_SOURCE\n') - 100
    current = source[:max(0, min(50000, remaining))]
    prompt += '\nCURRENT_SOURCE\n' + current
    if len(current) < len(source):
        prompt += '\n[Optional source excerpt truncated; recent measured feedback retained in full.]'
    require(len(prompt) <= MAX_PROMPT_CHARS and START + feedback + END in prompt,
            'Rendered prompt lost mandatory measured feedback')
    return prompt, {'feedback_protocol_version': 3, 'recent_first_ticket_ids': [r.get('ticket_id') for r in recent],
        'measured_feedback_sha256': digest(feedback.encode()), 'measured_feedback_chars': len(feedback),
        'prompt_chars': len(prompt), 'source_excerpt_chars': len(current),
        'source_chars_omitted': len(source) - len(current), 'shared_entries_omitted': len(shared) - len(chosen),
        'mandatory_measured_feedback_truncated': False}
