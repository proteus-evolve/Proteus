"""Reported usage at a native adapter boundary, never an implicit budget ledger."""

from proteus.core.contracts import Usage, require


DIMENSIONS = ('model_calls', 'input_tokens', 'output_tokens', 'cost_usd', 'wall_seconds')
RAW_SOURCES = frozenset(('codex_exec_jsonl', 'pi_sessions_v3', 'dsh_sessions_v0'))


def verify_usage_source(evidence, source):
    if evidence['source'] == 'codex_exec_jsonl':
        from proteus.core.native_codex_usage import verify_codex_source
        verify_codex_source(evidence, source)
    elif evidence['source'] == 'pi_sessions_v3':
        from proteus.core.native_pi_usage import verify_pi_source
        verify_pi_source(evidence, source)
    elif evidence['source'] == 'dsh_sessions_v0':
        from proteus.core.native_dsh_usage import verify_dsh_source
        verify_dsh_source(evidence, source)
    else:
        require(False, 'unsupported raw usage source')


def pi_usage(source, *, transport_complete):
    from proteus.core.native_pi_usage import pi_fields
    return usage_evidence('pi_sessions_v3', pi_fields(source, transport_complete=transport_complete))


def dsh_usage(source, *, transport_complete):
    from proteus.core.native_dsh_usage import dsh_fields
    return usage_evidence('dsh_sessions_v0', dsh_fields(source, transport_complete=transport_complete))


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


def evaluator_usage(reports, *, complete):
    return usage_evidence('evaluator_report', {'complete': complete, 'reports': reports})


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
    elif source == 'pi_sessions_v3':
        from proteus.core.native_pi_usage import pi_reported
        usage = pi_reported(fields, wall_seconds=wall_seconds)
    elif source == 'dsh_sessions_v0':
        from proteus.core.native_dsh_usage import dsh_reported
        usage = dsh_reported(fields, wall_seconds=wall_seconds)
    elif source == 'evaluator_report':
        require(set(fields) == {'complete', 'reports'} and type(fields['complete']) is bool
                and type(fields['reports']) is list, 'invalid native evaluator usage fields')
        dimensions = DIMENSIONS[:-1]
        for report in fields['reports']:
            require(type(report) is dict and set(report) == set(dimensions), 'invalid evaluator usage report')
            Usage.from_dict(report)
        totals = {key: sum_known(report[key] for report in fields['reports'])
                  if fields['complete'] and fields['reports'] else None for key in dimensions}
        usage = Usage(**totals, wall_seconds=wall_seconds)
    else:
        require(source in ('unreported', 'local_mock', 'local_evaluator') and not fields, 'unsupported native usage source')
        zero = 0 if source in ('local_mock', 'local_evaluator') else None
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
                     'codex_exec_jsonl': 'harness_cli', 'pi_sessions_v3': 'harness_cli',
                     'dsh_sessions_v0': 'harness_cli', 'local_evaluator': 'native_evaluator',
                     'evaluator_report': 'native_evaluator'}
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
        elif value['source'] == 'pi_sessions_v3':
            argv = request['payload']
            require(type(argv) is list and all(type(item) is str for item in argv), 'invalid Pi invocation')
            args = argv[:-1]
            require('-p' in args and '--session-dir' in args and not
                    set(args).intersection(('--resume', '-r', '--continue', '-c', '--session', '--no-session')),
                    'Pi usage requires new persisted sessions')
        elif value['source'] == 'dsh_sessions_v0':
            argv = request['payload']
            require(type(argv) is list and len(argv) == 3 and all(type(item) is str for item in argv)
                    and argv[:2] == ['--profile', 'headless'], 'DSH usage requires a fresh headless invocation')
        elif value['source'] in ('local_evaluator', 'evaluator_report'):
            require(type(request['payload']) is dict and type(request['payload'].get('model_free')) is bool
                    and request['payload']['model_free'] == (value['source'] == 'local_evaluator'),
                    'evaluator usage source differs from its dispatch')
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
        current_sessions = []
        if evidence['source'] == 'codex_exec_jsonl' and evidence['fields']['session_id'] is not None:
            current_sessions = [evidence['fields']['session_id']]
        elif evidence['source'] in ('pi_sessions_v3', 'dsh_sessions_v0'):
            current_sessions = evidence['fields']['session_ids']
        for session in current_sessions:
            identity = (evidence['source'], session)
            require(identity not in sessions, 'fresh native attempts reused a session')
            sessions.add(identity)
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
            'Reported model_calls uses chat responses and explicit evaluator reports, not harness launches or tool turns; '
            'Codex tokens cover its completed fresh-thread JSON receipt; '
            'Pi tokens cover reported usage in newly created v3 session logs. '
            'DSH tokens cover fresh format-0 logs after fork seeds, with unknown auxiliary/retry gaps. '
            'Evaluator reports cover only usage not already recorded by adapter boundaries; '
            'custom reports are evaluator-supplied, not independently observed billing. '
            'wall_seconds sums boundary elapsed time, not total run time. '
            'Unknown components keep totals null; known_subtotal is incomplete. '
            'Unreported evaluator usage and uninstrumented internal calls remain unknown.'}
