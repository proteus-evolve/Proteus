"""Installed report CLI consumes actual single/multiple-worker runs without reexecution."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from proteus.examples.coral_search import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.study import StudyCase, StudySpec


def check():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        cases = []
        for workers in (1, 2):
            target = root/str(workers)
            path = prepare(target/'study', workers=workers)
            registry = builtin_registry()
            register(registry)
            recipe = registry.resolve(load_recipe(path))
            lock = registry.make_lock(recipe, [SourcePin(name='proteus',repository='https://example.invalid/coral-report-fixture',revision='0'*40)])
            (target/'lock.json').write_text(lock.to_json())
            subprocess.run([sys.executable,'-m','proteus.cli','recipe','run',str(path),
                '--plugin','proteus.examples.coral_search:register','--lock',str(target/'lock.json'),
                '--out',str(target/'run'),'--artifact-store',str(target/'study/artifact-store')],check=True,capture_output=True)
            report_path=target/'report.json'
            subprocess.run([sys.executable,'-m','proteus.methods.coral.report',str(target/'run'),
                            '--out',str(report_path)],check=True,capture_output=True)
            report=json.loads(report_path.read_text())
            assert report['worker_count']==workers and len(report['generations'])==2*workers
            assert report['final_cohorts'][0]['mean_score'] is None
            cases.append(StudyCase(condition=str(workers),run=str(workers)+'/run',seed=0,evaluation_ids=('test',)))
        spec=StudySpec(title='Offline single/multi-worker profiles',mode='reference_profiles',seeds=(0,),cases=tuple(cases))
        (root/'study.json').write_text(spec.to_json())
        subprocess.run([sys.executable,'-m','proteus.methods.coral.report',str(root/'study.json'),
            '--study','--include-private','--out',str(root/'comparison.json')],check=True,capture_output=True)
        report=json.loads((root/'comparison.json').read_text())
        assert report['comparison']['paired_deltas']==[]
        assert [row['worker_count'] for row in report['cooperation_rows']]==[1,2]
        assert all(row['quality']==1 and row['cost_usd'] is None for row in report['comparison']['rows'])
        print('Installed CORAL report: real process runs, hidden/private confirmations, generation accounting and descriptive single/multi-worker study passed.')


if __name__ == '__main__':
    check()
