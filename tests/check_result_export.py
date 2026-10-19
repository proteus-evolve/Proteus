"""Exercise installed result export without repository plugins or model dependencies."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from check_recipe_catalog import check_example
from proteus.recipes.export import report_run


def check(root):
    root = Path(root)
    check_example(root/'example', 'search-policy')
    run = root/'example/run'
    subprocess.run([sys.executable, '-m', 'proteus.cli', 'recipe', 'export', str(run),
                    '--out', str(root/'export'), '--include-private'], check=True, capture_output=True)
    report = json.loads((root/'export/report.json').read_text())
    assert len(report['tasks']) == 7 and len(report['candidates']) == 3
    assert next(row['score'] for row in report['tasks'] if row['visibility'] == 'hidden') == .8
    assert report['accounting']['actual']['evaluations'] == 7
    assert report['accounting']['actual']['model_calls'] is None
    public = report_run(run)
    assert all(row['score'] is None for row in public['tasks'] if row['visibility'] == 'hidden')
    assert (root/'export/tasks.csv').is_file() and (root/'export/role-stage-costs.csv').is_file()
    print('Installed result export: saved task scores, lineage, unknown costs and private visibility passed')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='proteus-export-package-') as directory:
        check(directory)
