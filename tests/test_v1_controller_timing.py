"""Controller timing does not turn replay, downtime or partial intervals into speedups."""

from dataclasses import replace
import json
import shutil
import subprocess
import sys

import pytest

from check_recipe_catalog import check_example
from proteus.core.serialization import ContractError, content_hash
from proteus.recipes import controller_timing
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.export import report_run
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE
from test_v1_plugins import stage_recipe
from test_v1_timing import DRIVER, setup


def test_measured_interval_excludes_completed_replays_and_is_read_only(tmp_path, monkeypatch):
    ticks = iter([0., 5., 1000., 1100.])
    monkeypatch.setattr(controller_timing, 'monotonic', lambda: next(ticks))
    check_example(tmp_path, 'search-policy')
    root = tmp_path/'run'
    before = {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    public = report_run(root)
    timing = public['timing']
    assert timing['coverage'] == 'complete_controller_intervals'
    assert timing['continuous_run_seconds'] == timing['controller_seconds'] == timing['known_controller_seconds'] == 5
    assert [row['wall_seconds'] for row in timing['intervals']] == [5, 100]
    assert timing['replayed_invocations'] == 1 and timing['unknown_intervals'] == 0
    assert report_run(root, include_private=True)['timing'] == timing
    assert before == {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert not any(path.startswith('private/') for path in public['evidence'])


@pytest.mark.parametrize('boundary', ['timing.begin', 'stage', 'timing.finish'])
def test_hard_exit_retains_unknown_controller_interval_after_resume(tmp_path, boundary):
    command = [sys.executable, '-c', DRIVER, str(tmp_path), boundary]
    stopped = subprocess.run([*command, 'run'], capture_output=True, text=True, timeout=30)
    assert stopped.returncode == 86, stopped.stdout+stopped.stderr
    for _ in range(2):
        resumed = subprocess.run([*command, 'resume'], capture_output=True, text=True, timeout=30)
        assert resumed.returncode == 0, resumed.stdout+resumed.stderr
    timing = report_run(tmp_path/'run')['timing']
    assert timing['coverage'] == 'partial_controller_intervals'
    assert timing['continuous_run_seconds'] is None and timing['controller_seconds'] is None
    assert timing['known_controller_seconds'] > 0 and timing['unknown_intervals'] == 1
    assert timing['intervals'][0]['wall_seconds'] is None
    assert timing['replayed_invocations'] == 1
    assert (tmp_path/'calls').read_text().splitlines() == ['call']


def test_exit_after_snapshot_before_timing_receipt_stays_unknown(tmp_path, monkeypatch):
    registry, recipe = setup()
    lock = registry.make_lock(recipe, [SOURCE])
    original = controller_timing.publish_bytes

    def fail_finish(path, data):
        if path.name.endswith('.finish.json'):
            raise OSError('fixture loss before timing publication')
        return original(path, data)

    monkeypatch.setattr(controller_timing, 'publish_bytes', fail_finish)
    with pytest.raises(OSError, match='fixture loss'):
        run_recipe(recipe, registry=registry, lock=lock, root=tmp_path)
    assert report_run(tmp_path)['timing']['continuous_run_seconds'] is None
    monkeypatch.setattr(controller_timing, 'publish_bytes', original)
    assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path, resume=True).status.value == 'completed'
    timing = report_run(tmp_path)['timing']
    assert timing['controller_seconds'] is None and timing['known_controller_seconds'] == 0
    assert timing['unknown_intervals'] == 1 and timing['replayed_invocations'] == 1


def test_legacy_run_and_first_recorded_resume_keep_unmeasured_prefix(tmp_path):
    registry, recipe = setup()
    lock = registry.make_lock(recipe, [SOURCE])
    run_recipe(recipe, registry=registry, lock=lock, root=tmp_path)
    shutil.rmtree(tmp_path/controller_timing.DIRECTORY)
    path = tmp_path/'execution-0001.json'
    record = json.loads(path.read_text())
    del record['controller_timing']
    path.write_text(json.dumps(record))
    assert report_run(tmp_path)['timing']['coverage'] == 'unavailable'
    run_recipe(recipe, registry=registry, lock=lock, root=tmp_path, resume=True)
    timing = report_run(tmp_path)['timing']
    assert timing['unmeasured_prefix'] and timing['coverage'] == 'partial_controller_intervals'
    assert timing['controller_seconds'] is None and timing['continuous_run_seconds'] is None


def test_resume_cannot_hide_deleted_timing_as_a_legacy_run(tmp_path):
    registry, recipe = setup()
    lock = registry.make_lock(recipe, [SOURCE])
    run_recipe(recipe, registry=registry, lock=lock, root=tmp_path)
    shutil.rmtree(tmp_path/controller_timing.DIRECTORY)
    with pytest.raises(ContractError, match='missing controller timing'):
        run_recipe(recipe, registry=registry, lock=lock, root=tmp_path, resume=True)
    assert not (tmp_path/'execution-0002.json').exists()


@pytest.mark.parametrize('target', ['start', 'snapshot', 'outcome', 'negative', 'sequence', 'removed'])
def test_corrupted_timing_bindings_are_rejected(tmp_path, target):
    check_example(tmp_path, 'search-policy')
    root = tmp_path/'run'
    directory = root/controller_timing.DIRECTORY
    if target == 'removed':
        shutil.rmtree(directory)
    elif target == 'sequence':
        (directory/'00000001.start.json').unlink()
    else:
        path = root/'execution-0001.json' if target == 'snapshot' else directory/(
            '00000001.start.json' if target == 'start' else '00000001.finish.json')
        value = json.loads(path.read_text())
        if target == 'start':
            value['manifest_hash'] = content_hash('other run')
        elif target == 'snapshot':
            value['controller_timing']['sequence'] = 99
        elif target == 'negative':
            value['wall_seconds'] = -1
            value['hash'] = content_hash({k:v for k,v in value.items() if k != 'hash'})
        else:
            value['wall_seconds'] += 1
        path.chmod(0o644)
        path.write_text(json.dumps(value))
    with pytest.raises(ContractError, match='controller timing|controller elapsed'):
        report_run(root)


def test_failed_method_keeps_measured_interval_without_completed_run_time(tmp_path):
    registry = builtin_registry()
    # A native non-journal method is still measured by the common runner.
    from proteus.core.plugins import EmptyOptions, Plugin
    from proteus.core.contracts import PluginRef, OperationStatus
    from proteus.core.methods import MethodResult
    from test_v1_evaluation import plugin

    class Failed:
        def run(self, services, state):
            return MethodResult(status=OperationStatus.FAILED, diagnostic='fixture')

    registry.register(Plugin(ref=PluginRef(name='fixture.failed', version='1'), kind='method',
        options=EmptyOptions, factory=lambda options: Failed(), source='proteus',
        implementation_hash=content_hash('failed'), capabilities=('run.resume',)))
    recipe = replace(stage_recipe(), method=plugin('fixture.failed'))
    lock = registry.make_lock(recipe, [SOURCE])
    for resume in (False, True):
        assert run_recipe(recipe, registry=registry, lock=lock, root=tmp_path, resume=resume).status.value == 'failed'
    timing = report_run(tmp_path)['timing']
    assert timing['controller_seconds'] > 0 and timing['continuous_run_seconds'] is None
    assert len(timing['intervals']) == 2 and timing['replayed_invocations'] == 0
