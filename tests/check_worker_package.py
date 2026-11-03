"""Installed wheel/sdist acceptance for persistent worker recipes."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from proteus.core.ledger import Ledger
from proteus.examples.worker_lifecycle import prepare, register
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import RunManifest, SourcePin


def check():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        recipe_path = prepare(root / "study")
        registry = builtin_registry()
        register(registry)
        recipe = registry.resolve(load_recipe(recipe_path))
        lock = registry.make_lock(recipe, [SourcePin(name="proteus", repository="https://example.invalid/package",
                                                    revision="0" * 40)])
        lock_path = root / "lock.json"
        lock_path.write_text(lock.to_json())
        run = root / "run"
        base = [sys.executable, "-m", "proteus.cli", "recipe"]
        args = [str(recipe_path), "--plugin", "proteus.examples.worker_lifecycle:register", "--lock", str(lock_path),
                "--out", str(run)]
        subprocess.run([*base, "run", *args], check=True, capture_output=True, text=True, timeout=60)
        manifest = RunManifest.from_json((run / "manifest.json").read_text())
        ledger = Ledger(run / "private/ledger.sqlite3", run_id=manifest.run_id,
                        config_hash=recipe.config_hash, lock_hash=manifest.lock_hash)
        before = ledger.events()
        launches = sorted(path.read_text() for path in (run / "workers").glob("*/workspace/launches.jsonl"))
        assert sorted(len(text.splitlines()) for text in launches) == [1, 2]
        assert len(ledger.entries()) == 3 and ledger.balance()["active_slots"] == 0
        results = [json.loads(path.read_text()) for path in (run / "private/workers").glob("*/*/completion.json")]
        assert sorted(result["status"] for result in results) == ["completed", "completed", "paused"]
        assert sum(result["resumed_from"] is not None for result in results) == 1
        subprocess.run([*base, "resume", *args], check=True, capture_output=True, text=True, timeout=60)
        assert ledger.events() == before
        assert sorted(path.read_text() for path in (run / "workers").glob("*/workspace/launches.jsonl")) == launches
        print("Installed worker lifecycle: two workspaces, durable pause, confirmed native continuation and no-repeat CLI resume passed.")


if __name__ == "__main__":
    check()
