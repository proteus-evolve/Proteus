"""Exercise installed result export without repository plugins or model dependencies."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from check_recipe_catalog import check_example
from proteus.recipes.export import report_run
from proteus.api.v1 import MeasurementSpec, MeasurementSurface


def check(root):
    root = Path(root)
    check_example(root/'example', 'search-policy')
    run = root/'example/run'
    subprocess.run([sys.executable, '-m', 'proteus.cli', 'recipe', 'export', str(run),
                    '--out', str(root/'export'), '--include-private'], check=True, capture_output=True)
    report = json.loads((root/'export/report.json').read_text())
    assert len(report['tasks']) == 7 and len(report['candidates']) == 3
    assert next(row['score'] for row in report['tasks'] if row['visibility'] == 'hidden') == .8
    assert report['timing']['continuous_run_seconds'] > 0
    assert report['timing']['controller_seconds'] == report['timing']['continuous_run_seconds']
    assert report['timing']['replayed_invocations'] == 1
    assert (root/'export/controller-intervals.csv').is_file()
    assert report['accounting']['actual']['evaluations'] == 7
    assert report['accounting']['actual']['model_calls'] is None
    public = report_run(run)
    assert all(row['score'] is None for row in public['tasks'] if row['visibility'] == 'hidden')
    assert (root/'export/tasks.csv').is_file() and (root/'export/role-stage-costs.csv').is_file()
    child = next(row for row in report['candidates'] if row['parent_ids'])
    spec = MeasurementSpec(file_surfaces=(MeasurementSurface(name='code',subdir='main.py',unit='top_level_def'),),
                           lineage_paths={'selected':(child['parent_ids'][0],child['candidate_id'])})
    (root/'measurements.json').write_text(spec.to_json())
    subprocess.run([sys.executable,'-m','proteus.cli','recipe','export',str(run),
                    '--out',str(root/'measured'),'--measurements',str(root/'measurements.json')],check=True,capture_output=True)
    measured = json.loads((root/'measured/report.json').read_text())
    assert measured['measurements']['spec'] == spec.to_dict()
    assert len(measured['measurements']['edges']) == 2 and measured['measurements']['paths'][0]['status'] == 'measured'
    assert all(row['score'] is None for row in measured['tasks'] if row['visibility']=='hidden')
    assert all((root/'measured'/name).is_file() for name in ('artifact-measurements.csv','lineage-edges.csv','lineage-paths.csv'))
    print('Installed result export: saved task scores, lineage, unknown costs and private visibility passed')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='proteus-export-package-') as directory:
        check(directory)
