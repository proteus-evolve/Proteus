"""Write editable, offline plugin projects without modifying the core registry."""

import keyword
from pathlib import Path
import re
import sys

from proteus.core.contracts import require

KINDS = ('method', 'stage', 'evaluator', 'subject')
TEMPLATES = Path(__file__).with_name('templates')


def scaffold(kind, name, destination):
    require(kind in KINDS, 'choose method, stage, evaluator or subject')
    require(type(name) is str and re.fullmatch(r'[a-z][a-z0-9_]{2,47}', name) is not None
            and not keyword.iskeyword(name), 'name must be a lowercase Python identifier, 3–48 characters')
    require(name not in {*sys.stdlib_module_names, 'proteus', 'prepare', 'check'},
            'plugin module cannot shadow Python, Proteus or the project scripts')
    root = Path(destination).absolute()
    require(not root.exists() and not root.is_symlink(), 'scaffold destination already exists')
    files = {name+'.py': TEMPLATES/(kind+'.py.tmpl'), 'prepare.py': TEMPLATES/'prepare.py.tmpl',
             'README.md': TEMPLATES/'README.md.tmpl', 'check.py': TEMPLATES/'check.py.tmpl'}
    rendered = {target: source.read_text().replace('{{NAME}}', name).replace('{{KIND}}', kind)
                for target, source in files.items()}
    rendered['.gitignore'] = '__pycache__/\nstudy/\nrun/\nlock.json\n'
    root.mkdir(parents=True, exist_ok=False)
    for target, data in rendered.items():
        with (root/target).open('x', encoding='utf-8') as output:
            output.write(data)
    return root
