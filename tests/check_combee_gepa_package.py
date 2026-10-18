"""Installed Combee GEPA search, evaluation accounting and no-repeat recovery."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from proteus.core.contracts import OperationStatus
from proteus.examples.combee_gepa import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def check():
    with TemporaryDirectory(prefix="proteus-combee-gepa-package-") as directory:
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
        result = run_recipe(
            recipe,
            registry=registry,
            lock=lock,
            root=root / "run",
            artifact_source=root / "artifact-store",
        )
        assert result.status == OperationStatus.COMPLETED, result.diagnostic
        first = json.loads((root / "run/execution-0001.json").read_text())
        summary = next(
            row["value"]
            for row in first["values"]
            if row["ref"]["kind"] == "proteus.gepa.summary@1"
        )
        assert summary["metric_calls"] == 22 and summary["reflection_calls"] == 4
        assert len(first["actor_calls"]) == 28 and len(first["rollouts"]) == 24
        assert len(first["candidates"]) == 2 and len(summary["accepted_ids"]) == 2
        assert all(call["usage"]["model_calls"] == 0 for call in first["actor_calls"])
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
            == 28
        )
    print(
        "Installed Combee GEPA method, acceptance, hidden evaluation and call-free resume passed; no inference."
    )


if __name__ == "__main__":
    check()
