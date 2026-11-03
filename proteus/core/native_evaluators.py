"""Native evaluator boundary receipts without changing legacy score records."""

from contextlib import contextmanager
from dataclasses import asdict, replace
from threading import Lock
from types import CodeType, FunctionType, SimpleNamespace

from proteus.core.contracts import Usage, require
from proteus.core.native_inputs import native_input
from proteus.core.native_usage import DIMENSIONS, evaluator_usage, usage_evidence
from proteus.core.serialization import content_hash


def model_free(evaluator):
    """Recognize the actual bundled pure closures; copied labels do not qualify."""
    from proteus.core import evaluators
    if type(evaluator) is not FunctionType or evaluator.__globals__ is not vars(evaluators):
        return False
    for name in ('surface_units', 'tool_calls', 'file_contains', 'structural_step'):
        factory = getattr(evaluators, name)
        if type(factory) is FunctionType and any(value is evaluator.__code__
            for value in factory.__code__.co_consts if type(value) is CodeType):
            return True
    return False


@contextmanager
def native_evaluator(evaluator, trace, ctx, *, name, kind, visibility):
    """Record dispatch before calling user code; keep its score/output elsewhere."""
    if ctx.native_root is None:
        yield ctx
        return
    free = model_free(evaluator)
    payload = {'name': name, 'kind': kind, 'visibility': visibility,
        'callable_module': getattr(evaluator, '__module__', type(evaluator).__module__),
        'callable_name': getattr(evaluator, '__qualname__', type(evaluator).__qualname__),
        'harness_root': ctx.harness_root, 'trace_events': len(trace),
        'trace_hash': content_hash([asdict(event) for event in trace]), 'model_free': free}
    request = SimpleNamespace(root=ctx.native_root, episode=ctx.episode)
    reports, active, completed = [], True, False
    lock = Lock()

    def report(usage):
        require(type(usage) is Usage, 'evaluator usage must be a Usage record')
        with lock:
            require(active, 'evaluator usage boundary is already closed')
            require(not free, 'bundled model-free evaluator cannot report inference usage')
            reports.append({key: getattr(usage, key) for key in DIMENSIONS if key != 'wall_seconds'})

    with native_input(request, backend='evaluator', phase=None, scope='native_evaluator', payload=payload) as receipt:
        try:
            yield replace(ctx, _usage_recorder=report)
            completed = True
        finally:
            with lock:
                active = False
                receipt['usage'] = usage_evidence('local_evaluator') if free else evaluator_usage(reports, complete=completed)
