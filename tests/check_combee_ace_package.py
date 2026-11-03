"""Installed fixed-batch Combee-ACE through Python and CLI, without inference."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from proteus.core.contracts import OperationStatus
from proteus.examples.ace_playbook import register as register_fixture
from proteus.examples.combee_ace import prepare
from proteus.methods.ace.batch import register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin
from proteus.recipes.runner import run_recipe


def verify(root, mode):
    first, second = [json.loads((root / f"execution-{index:04d}.json").read_text()) for index in (1, 2)]
    for key in ("actor_calls", "rollouts", "evaluations", "candidates", "values"):
        assert first[key] == second[key], key
    values = {row["ref"]["id"]: row["value"] for row in first["values"]}
    summary = next(row["value"] for row in first["values"]
                   if row["ref"]["kind"] == "proteus.combee.ace_summary@1")
    batches = [values[ref["id"]] for ref in summary["batches"]]
    assert [row["task_ids"] for row in batches] == [["train-0", "train-1"], ["train-2"]]
    assert not any(row["rolled_back"] for row in batches)
    for batch in batches:
        samples = [values[ref["id"]] for ref in batch["samples"]]
        assert all(row["before_id"] == batch["before_id"] for row in samples)
        post = values[batch["post_evaluation"]["id"]]
        assert post["candidate_ids"] == [batch["after_id"]] * len(samples)
        assert all(row["score"] == 1 for evaluated in post["batches"] for row in evaluated["rows"])
    if mode == "offline":
        assert summary["selected_id"] == batches[0]["after_id"]
        assert len(summary["validations"]) == 1
        assert set(summary["initial_tests"]) == set(summary["final_tests"]) == {"test"}
    else:
        assert len(summary["windows"]) == 2 and not summary["final_tests"]
        assert all(not values[ref["id"]]["batches"] for ref in summary["windows"])
    assert all(row["usage"]["model_calls"] == 0 for row in first["actor_calls"])
    assert all("hidden-reference" not in row["request"]["prompt"] for row in first["actor_calls"])
    assert sum(len(path.read_text().splitlines()) for path in root.rglob("fixture-invocations.txt")) == len(first["actor_calls"])


def check():
    for mode in ("offline", "online"):
        for workers in (1, 3):
            with TemporaryDirectory(prefix="proteus-combee-ace-package-") as directory:
                root = Path(directory)
                path = prepare(root / "study", mode=mode, workers=workers)
                recipe = load_recipe(path)
                registry = builtin_registry()
                register(registry)
                register_fixture(registry)
                lock = registry.make_lock(recipe, [SourcePin(name="proteus",
                    repository="https://example.invalid/package", revision="0" * 40)])
                result = run_recipe(recipe, registry=registry, lock=lock, root=root / "run",
                                    artifact_source=root / "study/artifact-store")
                assert result.status == OperationStatus.COMPLETED, result.diagnostic
                assert run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=True) == result
                verify(root / "run", mode)
                if workers == 3:
                    lock_path = root / "study/lock.json"
                    lock_path.write_text(lock.to_json())
                    for action in ("run", "resume"):
                        command = [sys.executable, "-m", "proteus.cli", "recipe", action, str(path),
                            "--plugin", "proteus.methods.ace.batch:register",
                            "--plugin", "proteus.examples.ace_playbook:register",
                            "--lock", str(lock_path), "--out", str(root / "cli-run")]
                        if action == "run":
                            command += ["--artifact-store", str(root / "study/artifact-store")]
                        completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
                        assert completed.returncode == 0, completed.stdout + completed.stderr
                    verify(root / "cli-run", mode)
    print("Installed Combee-ACE: offline/online, serial/parallel, API/CLI and call-free resume passed; no inference.")


if __name__ == "__main__":
    check()
