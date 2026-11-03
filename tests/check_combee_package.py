"""Installed aggregation operators and no-repeat recovery outside the checkout."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from proteus.core.contracts import OperationStatus
from proteus.examples.combee_aggregation import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check(family="ace"):
    with TemporaryDirectory(prefix="proteus-combee-package-") as directory:
        root = Path(directory) / "study"
        recipe = load_recipe(prepare(root, family=family))
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
            artifact_source=root / "artifact-store" if family == "ace" else None,
        )
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        first = json.loads((root / "run/execution-0001.json").read_text())
        assert len(first["actor_calls"]) == 4
        assert sum(call["usage"]["model_calls"] for call in first["actor_calls"]) == 0
        plans = [
            row["value"] for row in first["values"] if row["ref"]["kind"] == "proteus.combee.plan@1"
        ]
        assert len(plans) == 1 and len(plans[0]["records"]) == 9
        assert sum(map(len, plans[0]["groups"])) == 18
        assert (
            run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=True)
            == result
        )
        second = json.loads((root / "run/execution-0002.json").read_text())
        assert first["actor_calls"] == second["actor_calls"]
        assert (
            sum(
                len(path.read_text().splitlines())
                for path in (root / "run").rglob("fixture-invocations.txt")
            )
            == 4
        )
    print(
        f"Installed {family} aggregation, occurrence provenance and call-free resume passed; no inference."
    )


if __name__ == "__main__":
    import sys

    check(sys.argv[1] if len(sys.argv) > 1 else "ace")
