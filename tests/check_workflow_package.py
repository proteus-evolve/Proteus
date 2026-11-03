"""Exercise installed adaptive programs and verify call-free finished recovery."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from proteus.core.contracts import OperationStatus
from proteus.examples.adaptive_workflows import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check():
    with TemporaryDirectory(prefix="proteus-workflow-package-") as directory:
        root = Path(directory)
        recipe = load_recipe(prepare(root / "study"))
        registry = builtin_registry()
        register(registry)
        lock = registry.make_lock(
            recipe,
            [
                SourcePin(
                    name="proteus", repository="https://example.invalid/package", revision="0" * 40
                )
            ],
        )
        result = run_recipe(
            recipe,
            registry=registry,
            lock=lock,
            root=root / "run",
            artifact_source=root / "study/artifact-store",
        )
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        first = json.loads((root / "run/execution-0001.json").read_text())
        assert [row["output"] for row in first["rollouts"]] == [0, 1, 0, 1, 2, 0, 1, 2, 3]
        assert len(first["candidates"]) == 7 and len(first["actor_calls"]) == 6
        assert all(row["usage"]["model_calls"] == 0 for row in first["actor_calls"])
        assert (
            run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=True)
            == result
        )
        second = json.loads((root / "run/execution-0002.json").read_text())
        assert (
            first["actor_calls"] == second["actor_calls"]
            and first["rollouts"] == second["rollouts"]
        )
        assert len((root / "study/fixture-invocations.txt").read_text().splitlines()) == 9
        lock_path = root / "study/lock.json"
        lock_path.write_text(lock.to_json())
        for action in ("run", "resume"):
            command = [
                sys.executable,
                "-m",
                "proteus.cli",
                "recipe",
                action,
                str(root / "study/recipe.json"),
                "--plugin",
                "proteus.examples.adaptive_workflows:register",
                "--lock",
                str(lock_path),
                "--out",
                str(root / "cli-run"),
            ]
            if action == "run":
                command += ["--artifact-store", str(root / "study/artifact-store")]
            done = subprocess.run(command, capture_output=True, text=True, timeout=60)
            assert done.returncode == 0, done.stdout + done.stderr
        cli = json.loads((root / "cli-run/execution-0002.json").read_text())
        assert [row["output"] for row in cli["rollouts"]] == [0, 1, 0, 1, 2, 0, 1, 2, 3]
        assert len(cli["actor_calls"]) == 6
        assert len((root / "study/fixture-invocations.txt").read_text().splitlines()) == 18
    print(
        "Installed adaptive workflows: isolated artifacts, formal evaluations, ordered joins and call-free resume passed; no inference."
    )


if __name__ == "__main__":
    check()
