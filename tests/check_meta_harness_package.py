"""Installed-package Meta-Harness recipe, resources, audit and call-free resume."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from proteus.core.contracts import OperationStatus
from proteus.examples.meta_harness_fixture import prepare, registry
from proteus.methods.meta_harness import reference
from proteus.methods.meta_harness.report import report
from proteus.recipes import load_recipe
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check():
    package = Path(reference.__file__).parent.parent
    for name in ("PROPOSER.md", "LICENSE-META-HARNESS.txt", "NOTICE.md"):
        assert (package / name).is_file(), name
    with TemporaryDirectory(prefix="proteus-meta-package-") as directory:
        root = Path(directory) / "study"
        recipe = load_recipe(prepare(root, task_count=1))
        reg = registry()
        lock = reg.make_lock(
            recipe,
            [
                SourcePin(
                    name="proteus",
                    repository="https://example.invalid/package-fixture",
                    revision="0" * 40,
                )
            ],
        )
        result = run_recipe(
            recipe,
            registry=reg,
            lock=lock,
            root=root / "run",
            artifact_source=root / "artifact-store",
        )
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        checked = report(root / "run", reg)
        assert checked["selected"] == ["fewshot_all", "learned_1", "no_memory"]
        assert len(checked["tests"]) == 3 and checked["ledger"]["charged"]["evaluations"] == 11
        calls = len(json.loads((root / "run/execution-0001.json").read_text())["actor_calls"])
        assert run_recipe(recipe, registry=reg, lock=lock, root=root / "run", resume=True) == result
        assert (
            len(json.loads((root / "run/execution-0002.json").read_text())["actor_calls"]) == calls
        )
    print("Installed Meta-Harness fixture, private audit and resume passed; no inference.")


if __name__ == "__main__":
    check()
