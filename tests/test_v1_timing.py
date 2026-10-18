"""Elapsed scopes retain committed measurements and reject interrupted durations."""

from dataclasses import replace
import json
import subprocess
import sys

import pytest

from proteus.core.contracts import OperationStatus, PluginRef
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import content_hash
from proteus.recipes.builtins import Text, builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE, plugin
from test_v1_plugins import stage_recipe


class TimedMethod:
    def run(self, services, restored_state):
        start = services.begin_timing("ordinary-iteration")
        prompt = services.values.publish("proteus.text@1", Text(text="timed work"))
        services.stage("propose", {"prompt": prompt})
        measured = services.finish_timing(start)
        return MethodResult(outputs={"timing": measured})


def setup():
    registry = builtin_registry()
    registry.register(Plugin(ref=PluginRef(name="fixture.timing", version="1"), kind="method",
        options=EmptyOptions, factory=lambda options: TimedMethod(), source="proteus",
        implementation_hash=content_hash("timed-fixture"), capabilities=("run.journal", "run.resume"),
        state_schema=EmptyOptions))
    return registry, replace(stage_recipe(), method=plugin("fixture.timing"))


DRIVER = r'''
import json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v1_timing import setup
from test_v1_evaluation import SOURCE
from proteus.recipes.builtins import EchoActor
from proteus.recipes.runner import run_recipe
from proteus.core.recovery import Journal
root,boundary,mode=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
root.mkdir(exist_ok=True)
original=EchoActor.execute
def execute(self,*args,**kwargs):
    with (root/'calls').open('a') as stream:stream.write('call\n')
    return original(self,*args,**kwargs)
EchoActor.execute=execute
if mode=='run':
    call=Journal.call
    def crash(self,kind,*args,**kwargs):
        result=call(self,kind,*args,**kwargs)
        if kind==boundary:os._exit(86)
        return result
    Journal.call=crash
registry,recipe=setup()
result=run_recipe(recipe,registry=registry,lock=registry.make_lock(recipe,[SOURCE]),root=root/'run',resume=mode=='resume')
records=json.loads(sorted((root/'run').glob('execution-*.json'))[-1].read_text())
timing=next(row['value'] for row in records['values'] if row['ref']['kind']=='proteus.timing.measurement@1')
print(json.dumps({'status':result.status.value,'timing':timing}))
'''


@pytest.mark.parametrize("boundary", ["timing.begin", "stage", "timing.finish"])
def test_hard_exit_never_profiles_replay_as_live_elapsed_time(tmp_path, boundary):
    def invoke(mode):
        return subprocess.run([sys.executable, "-c", DRIVER, str(tmp_path), boundary, mode],
                              text=True, capture_output=True, timeout=30)

    stopped = invoke("run")
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    resumed = invoke("resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    result = json.loads(resumed.stdout)
    assert result["status"] == OperationStatus.COMPLETED.value
    assert result["timing"]["interrupted"] == (boundary != "timing.finish")
    if boundary == "timing.finish":
        assert result["timing"]["wall_seconds"] > 0
    else:
        assert result["timing"]["wall_seconds"] is None
    again = invoke("resume")
    assert again.returncode == 0 and again.stdout == resumed.stdout
    assert (tmp_path / "calls").read_text().splitlines() == ["call"]


def test_continuous_measurement_survives_call_free_resume(tmp_path):
    registry, recipe = setup()
    lock = registry.make_lock(recipe, [SOURCE])
    first = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / "run")
    second = run_recipe(recipe, registry=registry, lock=lock, root=tmp_path / "run", resume=True)
    assert first == second and first.status == OperationStatus.COMPLETED
    records = json.loads((tmp_path / "run/execution-0002.json").read_text())
    timing = next(row["value"] for row in records["values"] if row["ref"]["kind"] == "proteus.timing.measurement@1")
    assert timing["wall_seconds"] > 0 and not timing["interrupted"]
    assert len(records["actor_calls"]) == 1
