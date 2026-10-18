"""Run the installed map/join example outside a source checkout without inference."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from proteus.core.contracts import OperationStatus
from proteus.core.parallel import StageMapResult
from proteus.examples.parallel_stages import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check():
    with TemporaryDirectory(prefix="proteus-parallel-package-") as directory:
        root = Path(directory) / "study"
        recipe = load_recipe(prepare(root))
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
        result = run_recipe(recipe, registry=registry, lock=lock, root=root / "run")
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        first = json.loads((root / "run/execution-0001.json").read_text())
        joined = next(
            StageMapResult.from_dict(row["value"])
            for row in first["values"]
            if row["ref"]["kind"] == "proteus.stage_map@1"
        )
        assert joined.succeeded and joined.keys == ("input-0", "input-1", "input-2")
        assert len(first["actor_calls"]) == 4
        assert sum(row["usage"]["model_calls"] for row in first["actor_calls"]) == 0
        assert (
            run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=True)
            == result
        )
        second = json.loads((root / "run/execution-0002.json").read_text())
        assert second["actor_calls"] == first["actor_calls"]
    print("Installed parallel map/join/reduce and settled resume passed; no inference.")


if __name__ == "__main__":
    check()
