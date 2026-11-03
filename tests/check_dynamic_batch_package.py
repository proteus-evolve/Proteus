"""Installed dynamic Combee recipes through API/CLI and call-free recovery."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from proteus.core.contracts import OperationStatus
from proteus.core.serialization import content_hash
from proteus.operators.combee.batching import DynamicBatchOptions
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def verify(root, family, mode):
    first, second = [json.loads((root / f"execution-{index:04d}.json").read_text()) for index in (1, 2)]
    for key in ("actor_calls", "rollouts", "evaluations", "candidates", "values"):
        assert first[key] == second[key], key
    values = {row["ref"]["id"]: row["value"] for row in first["values"]}
    kind = "proteus.combee.ace_summary@1" if family == "ace" else "proteus.gepa.summary@1"
    summary = next(row["value"] for row in first["values"] if row["ref"]["kind"] == kind)
    controller = summary["batch_controller"]
    assert controller["profile_sizes"] == [1, 2]
    assert len(controller["observations"]) == 2 and controller["selected_size"] in (1, 2)
    for row in controller["observations"]:
        elapsed = values[row["measurement"]["id"]]
        assert content_hash(elapsed) == row["measurement"]["content_hash"]
        assert not elapsed["interrupted"] and elapsed["wall_seconds"] == row["wall_seconds"]
        if row["actual_size"] < row["requested_size"]:
            assert row["epoch_seconds"] is None and "short batch" in row["diagnostic"]
        else:
            assert row["epoch_seconds"] > 0 and not row["diagnostic"]
    if family == "ace":
        batches = [values[ref["id"]] for ref in summary["batches"]]
        assert [task for batch in batches for task in batch["task_ids"]] == [f"train-{i}" for i in range(3)]
        assert [len(batch["task_ids"]) for batch in batches] == ([1, 2] if mode == "offline" else [1, 1, 1])
        assert [batch["timing"] for batch in batches[:2]] == [row["measurement"] for row in controller["observations"]]
    else:
        assert summary["metric_calls"] + 2 == len(first["rollouts"])
        assert len([row for row in first["values"] if row["ref"]["kind"] == "proteus.timing.measurement@1"]) == summary["iterations"]
    assert all(row["usage"]["model_calls"] == 0 for row in first["actor_calls"])
    assert sum(len(path.read_text().splitlines()) for path in root.rglob("fixture-invocations.txt")) == len(first["actor_calls"])


def check(family):
    if family == "ace":
        from proteus.examples.combee_ace import prepare
        from proteus.examples.ace_playbook import register as fixture
        from proteus.methods.ace.batch import register
        plugins = ("proteus.methods.ace.batch:register", "proteus.examples.ace_playbook:register")
        modes = ("offline", "online")
    else:
        from proteus.examples.combee_gepa import prepare, register
        def fixture(registry):
            pass
        plugins = ("proteus.examples.combee_gepa:register",)
        modes = (None,)
    for mode in modes:
        with TemporaryDirectory(prefix=f"proteus-dynamic-{family}-package-") as directory:
            root = Path(directory)
            kwargs = {"mode": mode, "workers": 3} if mode else {}
            path = prepare(root / "study", dynamic_batch=DynamicBatchOptions(max_batch_size=2), **kwargs)
            recipe = load_recipe(path)
            registry = builtin_registry()
            register(registry)
            fixture(registry)
            lock = registry.make_lock(recipe, [SourcePin(name="proteus",
                repository="https://example.invalid/package", revision="0" * 40)])
            result = run_recipe(recipe, registry=registry, lock=lock, root=root / "run",
                                artifact_source=root / "study/artifact-store")
            assert result.status == OperationStatus.COMPLETED, result.diagnostic
            assert run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=True) == result
            verify(root / "run", family, mode)
            # Generate a second study through the published example CLI, then run
            # its installed plugins via the public recipe CLI outside the checkout.
            command = [sys.executable, "-m", f"proteus.examples.combee_{family}",
                       "--out", str(root / "cli-study"), "--dynamic-max-batch-size", "2"]
            if mode:
                command += ["--mode", mode, "--workers", "3"]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            cli_path = root / "cli-study/recipe.json"
            cli_recipe = load_recipe(cli_path)
            cli_lock = registry.make_lock(cli_recipe, lock.sources)
            lock_path = root / "cli-study/lock.json"
            lock_path.write_text(cli_lock.to_json())
            for action in ("run", "resume"):
                command = [sys.executable, "-m", "proteus.cli", "recipe", action, str(cli_path),
                           "--lock", str(lock_path), "--out", str(root / "cli-run")]
                for plugin in plugins:
                    command += ["--plugin", plugin]
                if action == "run":
                    command += ["--artifact-store", str(root / "cli-study/artifact-store")]
                completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
                assert completed.returncode == 0, completed.stdout + completed.stderr
            verify(root / "cli-run", family, mode)
    print(f"Installed dynamic Combee {family}: profiling, API/CLI and call-free resume passed; no inference.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("family", choices=("ace", "gepa"))
    check(parser.parse_args().family)
