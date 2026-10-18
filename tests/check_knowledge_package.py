"""Installed knowledge recipe through the public CLI and historical read API."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from proteus.core.methods import MethodResult, RunServices
from proteus.core.serialization import content_hash
from proteus.examples.shared_knowledge import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import SourcePin


def check():
    with TemporaryDirectory(prefix="proteus-knowledge-package-") as folder:
        root = Path(folder)
        config = prepare(root / "study")
        registry = builtin_registry()
        register(registry)
        recipe = registry.resolve(load_recipe(config))
        lock = registry.make_lock(recipe, [SourcePin(name="proteus", repository="https://example.invalid/package",
                                                    revision="0" * 40)])
        lock_path = root / "lock.json"
        lock_path.write_text(lock.to_json())
        outputs = []
        for action in ("run", "resume"):
            result = subprocess.run([sys.executable, "-m", "proteus.cli", "recipe", action, str(config),
                "--plugin", "proteus.examples.shared_knowledge:register", "--lock", str(lock_path),
                "--out", str(root / "run"), "--artifact-store", str(root / "study/artifact-store")],
                text=True, capture_output=True, timeout=90)
            assert result.returncode == 0, result.stdout + result.stderr
            outputs.append(MethodResult.from_json(result.stdout))
        assert outputs[0] == outputs[1] and outputs[0].status.value == "completed"
        services = RunServices(run_id="run", recipe=recipe, registry=registry, root=root / "run", lock_hash=content_hash(lock))
        knowledge = services.knowledge("research").store
        before, after = (knowledge.get_snapshot(outputs[0].outputs[name]) for name in ("before", "after"))
        assert len(before.revisions) == 2 and len(after.revisions) == 3
        assert [row.revision for row in knowledge.history("notes", "result")] == [1, 2]
        for name, snapshot in (("before", before), ("after", after)):
            knowledge.materialize(snapshot, root / name)
        assert (root / "before/notes/result/note.md").read_bytes() != (root / "after/notes/result/note.md").read_bytes()
        records = [json.loads((root / "run" / f"execution-{index:04d}.json").read_text()) for index in (1, 2)]
        assert records[0]["rollouts"] == records[1]["rollouts"] and len(records[1]["rollouts"]) == 3
        view = json.loads((root / "after/attempts/initial/view.json").read_text())
        assert {row["evaluation_id"] for row in view["entries"]} == {None, "validation"}
    print("Installed shared knowledge: locked CLI recipe, historical snapshots, released evidence and call-free resume passed.")


if __name__ == "__main__":
    check()
