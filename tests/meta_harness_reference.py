"""Execute pinned upstream functions/classes, excluding only unused SDK imports."""

import ast
from collections import defaultdict
from contextlib import contextmanager
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys

from check_meta_harness_code_reference import reference_files


HASHES = {
    "inner_loop.py": "0c121c567180cdbf63639c18327dc7877de4570415385672d60b2cc833fe4216",
    "benchmark.py": "9f1f924c331f7aeee7f8a7d99b8e38b1773b4cf99e497a80b2c7ccd3e3cdbdb6",
    "data/api.py": "1aabf00666eb0195f1b01b11ba766cd7b7250701139f847fba3c36d221676d53",
    "data/constants.py": "79ae1bd45dc33a473d6d1a5849a1182afd5b219fd6005a88a8668ff302b5cf87",
    "data/loaders.py": "96f88a636c88d3983e9604467dc173acaef9d095fadafa152d7811801f0ae768",
    "data/evaluators.py": "e4f20042e4cc526da5f7d12ee8f74d0c09bdbaa99243aef2b904cfca0a7215aa",
}


def checked(source, name):
    raw = (Path(source) / "reference_examples/text_classification" / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == HASHES[name], name
    return raw.decode()


@contextmanager
def reference(source, root):
    root = Path(root)
    for name, value in reference_files(source).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.data)
    package = root / "meta_reference_fixture"
    for name in (
        "inner_loop.py",
        "data/constants.py",
        "data/evaluators.py",
        "data/api.py",
        "data/loaders.py",
    ):
        code = checked(source, name)
        if name == "data/loaders.py":
            # All MCE loader function bodies stay byte-for-byte unchanged.
            code = code.replace("from datasets import load_dataset\n", "")
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code)
    (package / "data/__init__.py").write_text("")
    (package / "agents/helper.py").write_text("def identity(value):\n    return value\n")
    sys.path.insert(0, str(root))
    try:
        inner = importlib.import_module("meta_reference_fixture.inner_loop")
        loaders = importlib.import_module("meta_reference_fixture.data.loaders")
        loaders.MCE_DATA_PATH = str(Path(source) / "reference_examples/text_classification/data")
        api = importlib.import_module("meta_reference_fixture.data.api")
        yield inner, api
    finally:
        sys.path.remove(str(root))
        for name in tuple(sys.modules):
            if name == "meta_reference_fixture" or name.startswith("meta_reference_fixture."):
                del sys.modules[name]


def benchmark_functions(source, datasets, seeds):
    tree = ast.parse(checked(source, "benchmark.py"))
    names = {
        "run_dir",
        "parse_run_path",
        "load_results",
        "compute_pareto_frontier",
        "print_frontier",
    }
    definitions = ast.Module(
        body=[
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
        ],
        type_ignores=[],
    )
    assert {node.name for node in definitions.body} == names
    namespace = {
        "Path": Path,
        "json": json,
        "re": re,
        "defaultdict": defaultdict,
        "DATASETS": datasets,
        "SEEDS": seeds,
        "DEFAULT_SEED": 42,
    }
    exec(compile(definitions, "actual_pinned_meta_benchmark.py", "exec"), namespace)
    return namespace
