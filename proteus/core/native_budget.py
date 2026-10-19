"""Finite call caps for the bundled, bounded native adapters.

The legacy episode transaction may retry work, but every attempt keeps its own
reservation. An interrupted dispatch is never permission to refill or retry.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from proteus.core.contracts import Usage, require
from proteus.core.ledger import Amounts
from proteus.core.recovery import RecoveryRequired
from proteus.core.serialization import read_json


_current = ContextVar('proteus_native_budget', default=None)


def enabled(recipe):
    if recipe.method.plugin.name != 'proteus.native':
        return False
    role = recipe.method.options.get('actor', 'agent')
    return recipe.budget.max_model_calls is not None or recipe.actors[role].budget.max_model_calls is not None


def validate(recipe, options):
    if not enabled(recipe):
        return
    plugin = recipe.actors[options.actor].runtime.plugin.name
    require(plugin in ('proteus.native.minimal', 'proteus.native.llm'),
            'finite native model-call caps require the bundled minimal or llm adapter')
    require(all(spec.partition('@')[0].partition(':')[0] in ('units', 'tool-calls', 'step', 'contains')
                for spec in options.evaluators),
            'finite native model-call caps require bundled model-free evaluators')


def _identity(recipe, request):
    role = recipe.method.options.get('actor', 'agent')
    plugin = recipe.actors[role].runtime.plugin.name
    pair = (request['backend'], request['scope'])
    if pair == ('evaluator', 'native_evaluator'):
        require(request['payload'].get('model_free') is True, 'native evaluator has no model-call bound')
        bound = 0
    else:
        expected = {'proteus.native.minimal': ('minimal', 'policy_prompt', 0),
                    'proteus.native.llm': ('llm', 'chat_request', 1)}
        require(plugin in expected and pair == expected[plugin][:2], 'unexpected bounded native adapter boundary')
        bound = expected[plugin][2]
    return {'scopes': sorted(('run', f'role:{role}')),
        'reservation': Amounts(model_calls=bound, cost_usd=0 if bound == 0 else None).to_dict(),
        'slots': 1, 'metadata': {'kind': 'native_boundary', 'role': role,
            'stage': request['phase'], 'operation_id': request['id']}, 'request': request}


def _settlement(outcome):
    actual = Amounts.from_usage(Usage.from_dict(outcome['usage']['reported']))
    status = 'completed' if outcome['status'] == 'returned' else 'failed'
    result = {'input_hash': outcome['input_hash'], 'outcome_hash': outcome['hash']}
    return actual, status, result


def verify_scopes(recipe, db):
    """Check immutable limits and memberships in the same ledger read transaction."""
    role = recipe.method.options.get('actor', 'agent')
    limits = {'run': recipe.budget.to_dict(), f'role:{role}': recipe.actors[role].budget.to_dict()}
    scopes = {key: read_json(value) for key, value in db.execute('SELECT id, limits_json FROM scopes')}
    require(not scopes or scopes == limits, 'native ledger scope limits differ from recipe')
    for key, encoded in db.execute('SELECT id, identity_json FROM entries'):
        identity = read_json(encoded)
        members = sorted(row[0] for row in db.execute('SELECT scope FROM membership WHERE entry=?', (key,)))
        require(members == identity['scopes'] == sorted(limits), 'native ledger scope membership differs')


def verify(recipe, entries, records):
    """Bind saved accounting to every immutable boundary, without plugin execution."""
    entries = {entry['id']: entry for entry in entries if entry['identity']['metadata'].get('kind') == 'native_boundary'}
    require(enabled(recipe) or not entries, 'unexpected native budget ledger')
    if not enabled(recipe):
        return []
    from proteus.methods.native import NativeOptions
    validate(recipe, NativeOptions.from_dict(recipe.method.options))
    matched, bindings = set(), []
    for row in records:
        request, outcome = row['input'], row['outcome']
        key = 'native-'+request['id']
        entry = entries.get(key)
        require(entry is not None and entry['identity'] == _identity(recipe, request),
                'native budget request/reservation differs from receipt')
        require(outcome is not None, 'native budget dispatch has no durable outcome')
        actual, status, result = _settlement(outcome)
        require(entry['state'] == status and entry['settlement'] == {
            'actual': actual.to_dict(), 'status': status, 'result': result, 'violations': []},
            'native budget settlement differs from receipt')
        matched.add(key)
        bindings.append({'attempt_id': request['id'], 'entry_id': key,
                         'reserved_model_calls': entry['identity']['reservation']['model_calls']})
    for key, entry in entries.items():
        if key in matched:
            continue
        require(key == 'native-'+entry['identity']['request']['id']
                and entry['identity'] == _identity(recipe, entry['identity']['request'])
                and entry['state'] == 'cancelled' and entry['settlement'] == {
                    'actual': Amounts.zero().to_dict(), 'status': 'cancelled',
                    'result': None, 'violations': []}, 'native budget entry has no matching receipt')
    return bindings


class NativeBudget:
    def __init__(self, services):
        self.services = services
        self.recipe = services.recipe
        self.ledger = services.ledger
        self.root = (services.root/'workspace').resolve()

    def prepare(self, root, request):
        require(Path(root).resolve() == self.root, 'native budget scope escapes its workspace')
        identity = _identity(self.recipe, request)
        role = identity['metadata']['role']
        key = 'native-'+request['id']
        self.ledger.reserve(key, scopes={'run': self.recipe.budget, f'role:{role}': self.recipe.actors[role].budget},
            reservation=Amounts.from_dict(identity['reservation']), metadata=identity['metadata'],
            request=request, reuse=False)
        # Any crash after this point conservatively retains the reservation,
        # even if publishing the input or dispatching the transport never finishes.
        self.ledger.start(key)

    def finish(self, outcome):
        actual, status, result = _settlement(outcome)
        violations = self.ledger.settle('native-'+outcome['id'], actual=actual, status=status, result=result)
        require(not violations, 'native model-call usage exceeded its reserved bound')

    def recover(self):
        from proteus.core.native_inputs import read_native_inputs
        with self.ledger._connection() as db:
            db.execute('BEGIN')
            verify_scopes(self.recipe, db)
        records = read_native_inputs(self.root)
        current = {'native-'+row['input']['id']: row for row in records}
        uncertain = []
        for entry in self.ledger.entries():
            require(entry['identity']['metadata'].get('kind') == 'native_boundary',
                    'bounded native run contains unrelated ledger work')
            key, row = entry['id'], current.get(entry['id'])
            require(key == 'native-'+entry['identity']['request']['id']
                    and entry['identity'] == _identity(self.recipe, entry['identity']['request']),
                    'native recovery reservation differs')
            if entry['state'] == 'reserved':
                require(row is None, 'unstarted native reservation has a dispatch receipt')
                self.ledger.settle(key, actual=Amounts.zero(), status='cancelled')
            elif entry['state'] == 'started':
                if row is None or row['outcome'] is None:
                    uncertain.append(key)
                else:
                    require(row['input'] == entry['identity']['request'], 'native recovery input differs')
                    self.finish(row['outcome'])
        if uncertain:
            raise RecoveryRequired('native dispatch outcome is unknown; reservations retained: '+', '.join(uncertain))
        verify(self.recipe, self.ledger.entries(), records)


def current():
    return _current.get()


@contextmanager
def budget_scope(services, adapter, evaluators):
    if not enabled(services.recipe):
        yield
        return
    from proteus.adapters.llm import LLMHarness
    from proteus.adapters.minimal import MinimalHarness, mock_policy
    from proteus.core.native_evaluators import model_free
    require(type(adapter) is LLMHarness or (type(adapter) is MinimalHarness and adapter.policy is mock_policy),
            'finite native budget requires an unmodified bundled adapter')
    require(all(model_free(spec.run) for spec in evaluators), 'finite native budget requires model-free evaluators')
    require(_current.get() is None, 'nested native budget scope')
    budget = NativeBudget(services)
    budget.recover()
    token = _current.set(budget)
    try:
        yield
    finally:
        _current.reset(token)
