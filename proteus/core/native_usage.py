"""Reported usage at a native adapter boundary, never an implicit budget ledger."""

from proteus.core.contracts import Usage, require


DIMENSIONS = ('model_calls', 'input_tokens', 'output_tokens', 'cost_usd', 'wall_seconds')


def codex_usage(source, *, transport_complete):
    from proteus.core.native_codex_usage import codex_fields
    return usage_evidence('codex_exec_jsonl', codex_fields(source, transport_complete=transport_complete))


def chat_usage(raw):
    """Keep only nonnegative integral provider counts; absence/invalidity is unknown.

    Chat prompt/completion totals already include cache/reasoning subsets. Provider
    cost extensions are deliberately excluded: their billing semantics are unknown.
    """
    raw = raw if isinstance(raw, dict) else {}
    fields = {key: value if type(value := raw.get(key)) is int and value >= 0 else None
              for key in ('prompt_tokens', 'completion_tokens')}
    return usage_evidence('openai_chat_response', fields)


def usage_evidence(source='unreported', fields=None, *, wall_seconds=None):
    fields = {} if fields is None else fields
    require(type(fields) is dict, 'native usage fields must be an object')
    if source == 'openai_chat_response':
        require(set(fields) == {'prompt_tokens', 'completion_tokens'}, 'invalid native chat usage fields')
        usage = Usage(model_calls=1, input_tokens=fields['prompt_tokens'],
                      output_tokens=fields['completion_tokens'], wall_seconds=wall_seconds)
    elif source == 'codex_exec_jsonl':
        from proteus.core.native_codex_usage import codex_reported
        usage = codex_reported(fields, wall_seconds=wall_seconds)
    else:
        require(source in ('unreported', 'local_mock') and not fields, 'unsupported native usage source')
        zero = 0 if source == 'local_mock' else None
        usage = Usage(model_calls=zero, input_tokens=zero, output_tokens=zero,
                      cost_usd=zero, wall_seconds=wall_seconds)
    return {'version': 1, 'source': source, 'fields': fields, 'reported': usage.to_dict()}


def validate_usage(value, request=None):
    require(type(value) is dict and set(value) == {'version', 'source', 'fields', 'reported'}
            and type(value['version']) is int and value['version'] == 1, 'invalid native usage evidence')
    usage = Usage.from_dict(value['reported'])
    expected = usage_evidence(value['source'], value['fields'], wall_seconds=usage.wall_seconds)
    require(value == expected, 'native usage normalization differs')
    if request is not None:
        permitted = {'openai_chat_response': 'chat_request', 'local_mock': 'policy_prompt',
                     'codex_exec_jsonl': 'harness_cli'}
        require(value['source'] not in permitted or
                request['scope'] == permitted[value['source']],
                'native usage source does not match adapter boundary')
        if value['source'] == 'codex_exec_jsonl':
            argv = request['payload']
            require(type(argv) is list and all(type(item) is str for item in argv)
                    and 'exec' in argv, 'Codex usage needs an exec invocation')
            args = argv[argv.index('exec')+1:-1]
            require(args and args[0] == '--json' and '--ephemeral' in args,
                    'Codex native usage requires a fresh ephemeral JSON stream')
    return value


def sum_known(values):
    values = list(values)
    return None if None in values else sum(values)


def summarize_native_inputs(records):
    """Summarize distinct immutable attempts; never sum repeated resume snapshots."""
    identities, sessions, rows = set(), set(), []
    for record in records:
        request, outcome = record['input'], record['outcome']
        require(request['id'] not in identities, 'duplicate native usage attempt')
        identities.add(request['id'])
        evidence = validate_usage(outcome['usage'], request) if outcome and 'usage' in outcome else usage_evidence()
        if evidence['source'] == 'codex_exec_jsonl' and evidence['fields']['session_id'] is not None:
            session = evidence['fields']['session_id']
            require(session not in sessions, 'fresh native Codex attempts reused a session')
            sessions.add(session)
        rows.append({'attempt_id': request['id'], 'backend': request['backend'],
            'episode': request['episode'], 'phase': request['phase'], 'scope': request['scope'],
            'completion': record['completion'], 'returncode': outcome.get('returncode') if outcome else None,
            'source': evidence['source'], **{key: evidence['reported'][key] for key in DIMENSIONS}})
    return {'coverage': 'recorded_adapter_boundaries' if rows else 'unavailable',
        'boundary_count': len(rows), 'entries': rows,
        'reported': {key: sum_known(row[key] for row in rows) if rows else None for key in DIMENSIONS},
        'known_subtotal': {key: sum(row[key] for row in rows if row[key] is not None) for key in DIMENSIONS},
        'unknown_entries': {key: sum(row[key] is None for row in rows) for key in DIMENSIONS},
        'budget_enforced': False,
        'semantics': 'Cumulative recorded boundaries only; empty evidence is not zero usage. '
            'Reported model_calls counts chat responses, not harness launches or tool turns; '
            'Codex tokens cover its completed fresh-thread JSON receipt. '
            'wall_seconds sums boundary elapsed time, not total run time. '
            'Unknown components keep totals null; known_subtotal is incomplete. '
            'Native evaluators and uninstrumented internal calls may be outside this coverage.'}
