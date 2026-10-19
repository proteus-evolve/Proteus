"""Evaluator evidence covers scheduled attempts without changing legacy scores."""
from dataclasses import replace
from functools import wraps
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from proteus.core.adapter import ActionEvent
from proteus.core.contracts import Usage
from proteus.core.evaluators import file_contains, structural_step, surface_units, tool_calls
from proteus.core.goal import EvalResult, EvaluatorSpec, Goal, GoalConfig, GoalContext
from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.native_usage import summarize_native_inputs
from proteus.core.serialization import ContractError


def evaluate(root, evaluator, *, kind='measurement'):
    (root/'harness').mkdir(exist_ok=True)
    goal = GoalConfig.of(evaluators=(EvaluatorSpec('judge', evaluator, kind=kind),))
    result = goal.evaluate([ActionEvent(1, 'act', text='PRIVATE-TRACE')],
                          GoalContext(str(root/'harness'), 1, native_root=str(root)))
    return result[0], read_native_inputs(root)[-1]


@pytest.mark.parametrize('factory', [lambda: surface_units('notes'), tool_calls,
    lambda: file_contains('notes.md', 'private'), lambda: structural_step(())])
def test_bundled_measurements_have_zero_inference_but_no_private_result(tmp_path, factory):
    from proteus.core import snapshot
    (tmp_path/'harness').mkdir()
    snapshot.init(tmp_path/'harness')
    result, row = evaluate(tmp_path, factory())
    assert result.status == 'ok'
    assert row['input']['scope'] == 'native_evaluator' and row['input']['phase'] is None
    assert row['input']['payload']['trace_events'] == 1
    assert 'PRIVATE-TRACE' not in json.dumps(row)
    assert 'detail' not in row['input']['payload']
    usage = row['outcome']['usage']
    assert usage['source'] == 'local_evaluator'
    assert all(usage['reported'][key] == 0 for key in ('model_calls', 'input_tokens', 'output_tokens', 'cost_usd'))
    assert usage['reported']['wall_seconds'] >= 0


def test_failed_model_free_measurement_still_has_zero_inference(tmp_path):
    result, row = evaluate(tmp_path, structural_step(()))  # missing snapshot repository
    assert result.status == 'error' and row['completion'] == 'raised'
    assert row['outcome']['usage']['reported']['model_calls'] == 0


def test_copied_builtin_labels_and_measurement_kind_do_not_imply_zero(tmp_path):
    original = tool_calls()
    @wraps(original)
    def custom(trace, ctx):
        return original(trace, ctx)
    result, row = evaluate(tmp_path, custom)
    assert result.status == 'ok' and not row['input']['payload']['model_free']
    assert row['outcome']['usage']['source'] == 'evaluator_report'
    assert summarize_native_inputs([row])['reported']['model_calls'] is None


def test_reported_calls_have_independent_unknown_dimensions_and_a_sealed_callback(tmp_path):
    captured = []
    def custom(trace, ctx):
        captured.append(ctx)
        ctx.report_usage(Usage(model_calls=1, input_tokens=7, output_tokens=3, cost_usd=.2, wall_seconds=999))
        ctx.report_usage(Usage(model_calls=1, input_tokens=None, output_tokens=2, cost_usd=.1))
        return EvalResult('ignored', .8, detail='PRIVATE-RESULT')
    result, row = evaluate(tmp_path, custom)
    assert result.name == 'judge' and result.detail == 'PRIVATE-RESULT'
    assert 'PRIVATE-RESULT' not in json.dumps(row)
    usage = row['outcome']['usage']
    assert usage['fields']['complete'] and len(usage['fields']['reports']) == 2
    assert usage['reported']['model_calls'] == 2 and usage['reported']['input_tokens'] is None
    assert usage['reported']['output_tokens'] == 5 and usage['reported']['cost_usd'] == pytest.approx(.3)
    assert 0 <= usage['reported']['wall_seconds'] < 999
    with pytest.raises(ContractError, match='closed'):
        captured[0].report_usage(Usage(model_calls=0))


@pytest.mark.parametrize('failure', ['raise', 'invalid_score', 'invalid_report'])
def test_failure_preserves_reported_evidence_without_claiming_complete_totals(tmp_path, failure):
    def custom(trace, ctx):
        ctx.report_usage(Usage(model_calls=1, input_tokens=7))
        if failure == 'raise':
            raise RuntimeError('PRIVATE-EXCEPTION')
        if failure == 'invalid_report':
            ctx.report_usage({'model_calls': 1})
        return EvalResult('judge', float('nan'))
    result, row = evaluate(tmp_path, custom)
    assert result.status == 'error' and row['completion'] == 'raised'
    assert 'PRIVATE-EXCEPTION' not in json.dumps(row)
    usage = row['outcome']['usage']
    assert not usage['fields']['complete'] and usage['fields']['reports'][0]['input_tokens'] == 7
    assert usage['reported']['model_calls'] is None and usage['reported']['wall_seconds'] >= 0


def test_explicit_no_additional_usage_does_not_double_count_nested_adapter(tmp_path):
    from types import SimpleNamespace
    from proteus.core.native_inputs import native_input
    from proteus.core.native_usage import chat_usage
    def custom(trace, ctx):
        with native_input(SimpleNamespace(root=tmp_path, episode=1), backend='llm', phase='judge',
                          scope='chat_request', payload=[]) as receipt:
            receipt['usage'] = chat_usage({'prompt_tokens': 7, 'completion_tokens': 3})
        ctx.report_usage(Usage(model_calls=0, input_tokens=0, output_tokens=0, cost_usd=0))
        return EvalResult('judge', 1)
    evaluate(tmp_path, custom)
    usage = summarize_native_inputs(read_native_inputs(tmp_path))['reported']
    assert usage['model_calls'] == 1 and usage['input_tokens'] == 7 and usage['output_tokens'] == 3
    assert usage['cost_usd'] is None


def test_standalone_context_retains_no_write_behavior(tmp_path):
    ctx = GoalContext(str(tmp_path), 1)
    assert GoalConfig.of(evaluators=(EvaluatorSpec('calls', tool_calls()),)).evaluate([], ctx)[0].score == 0
    assert not input_directory(tmp_path).exists()
    with pytest.raises(RuntimeError, match='active native evaluator'):
        ctx.report_usage(Usage(model_calls=0))


@pytest.mark.parametrize('record', [False, True])
def test_existing_string_visibility_still_evaluates(tmp_path, record):
    goal = GoalConfig(goals=(Goal('legacy', evaluator=tool_calls(), visibility='hidden'),),
                      evaluators=(EvaluatorSpec('current', tool_calls(), visibility='observe'),))
    results = goal.evaluate([], GoalContext(str(tmp_path), 1, native_root=str(tmp_path) if record else None))
    assert len(results) == 2 and all(result.status == 'ok' for result in results)
    rows = read_native_inputs(tmp_path)
    assert [r['input']['payload']['visibility'] for r in rows] == (['hidden', 'observe'] if record else [])


def test_schedule_baseline_legacy_goal_and_no_repeat_resume(tmp_path):
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core import NEUTRAL
    from proteus.core.episode import RunConfig, run
    goal = GoalConfig(goals=(Goal('legacy', evaluator=tool_calls()),), evaluators=(
        EvaluatorSpec('scheduled', tool_calls(), episodes=(0, 2)),))
    cfg = RunConfig(name='usage', adapter=MinimalHarness(), disposition=NEUTRAL, goal=goal,
                    root=tmp_path/'run', model='mock', episodes=2, seed=1)
    result = run(cfg)
    assert result.episodes_complete == 2
    rows = read_native_inputs(cfg.root)
    evaluations = [row for row in rows if row['input']['scope'] == 'native_evaluator']
    assert [(row['input']['episode'], row['input']['payload']['name']) for row in evaluations] == [
        (0, 'scheduled'), (1, 'legacy'), (2, 'legacy'), (2, 'scheduled')]
    run(cfg, start=2, resume=True)
    assert read_native_inputs(cfg.root) == rows
    run(replace(cfg, episodes=3), start=2, resume=True)
    evaluations = [r for r in read_native_inputs(cfg.root) if r['input']['scope'] == 'native_evaluator']
    assert len(evaluations) == 5 and evaluations[-1]['input']['episode'] == 3


def test_hard_exit_retains_unknown_attempt_separately_from_retry(tmp_path):
    source = '''
import os, sys
from proteus.core.goal import GoalConfig, EvaluatorSpec, GoalContext
def killed(trace, ctx):
    os._exit(23)
GoalConfig.of(evaluators=(EvaluatorSpec('judge', killed),)).evaluate([], GoalContext('unused', 1, native_root=sys.argv[1]))
'''
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path)], env=environment)
    assert result.returncode == 23
    first = read_native_inputs(tmp_path)[0]
    assert first['completion'] == 'unknown' and first['outcome'] is None
    evaluate(tmp_path, tool_calls())
    rows = read_native_inputs(tmp_path)
    assert len(rows) == 2 and rows[0] == first and rows[0]['input']['id'] != rows[1]['input']['id']
    assert summarize_native_inputs(rows)['reported']['model_calls'] is None
