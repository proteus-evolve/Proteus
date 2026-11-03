"""Run from outside the repository against an installed wheel or sdist."""

import importlib.resources
import json
import tempfile
from pathlib import Path

from proteus.artifacts.store import ArtifactStore
from proteus.methods.ace.playbook import CODEC, Playbook, PlaybookDelta, register
from proteus.recipes.builtins import builtin_registry


def check():
    resources = importlib.resources.files("proteus.methods.ace")
    assert "Apache License" in resources.joinpath("LICENSE-ACE.txt").read_text()
    assert "82709de050e1db6e6ef2f07bcb0393560b94992a" in resources.joinpath("NOTICE.md").read_text()
    registry = builtin_registry()
    register(registry)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = ArtifactStore(root / "artifacts", registry)
        initial = store.put(CODEC, Playbook.from_text())
        added = store.apply_delta(initial, PlaybookDelta(base_hash=initial.content_hash, kind="curate",
            records=({"type": "ADD", "section": "OTHERS", "content": "keep units consistent"},)))
        changed = store.apply_delta(added, PlaybookDelta(base_hash=added.content_hash, kind="counters",
            records=({"id": "misc-00001", "tag": "helpful"},)))
        reloaded = ArtifactStore(store.root, registry).get(changed)
        assert reloaded.next_id == 2 and reloaded.bullets[0].helpful == 1
        assert store.get(initial).bullets == ()
        destination = store.materialize(changed, root / "candidate")
        assert (destination / "playbook.txt").read_text() == reloaded.text
    print("Installed ACE codec, reference functions, immutable deltas and licenses: passed")
    from proteus.examples.ace_playbook import prepare, register as register_fixture
    from proteus.methods.ace.method import register as register_method
    from proteus.recipes import load_recipe
    from proteus.recipes.manifest import SourcePin
    from proteus.recipes.runner import run_recipe
    for mode, calls, trials in (("offline", 22, 16), ("online", 19, 13)):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "study"
            recipe = load_recipe(prepare(root, mode=mode))
            registry = builtin_registry()
            register_method(registry)
            register_fixture(registry)
            lock = registry.make_lock(recipe, [SourcePin(name="proteus", repository="https://example.invalid/fixture",
                                                        revision="0" * 40)])
            result = run_recipe(recipe, registry=registry, lock=lock, root=root / "run", artifact_source=root / "artifact-store")
            assert result.status.value == "completed", result.diagnostic
            assert run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=True) == result
            records = json.loads((root / "run/execution-0002.json").read_text())
            assert len(records["actor_calls"]) == calls and len(records["rollouts"]) == trials
            assert sum(len(path.read_text().splitlines()) for path in (root / "run").rglob("fixture-invocations.txt")) == calls
        print(f"Installed ACE {mode} method, prompts, parsers, roles and repeated resume: passed")


if __name__ == "__main__":
    check()
