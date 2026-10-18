"""Run from outside the repository against an installed wheel or sdist."""

import importlib.resources
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


if __name__ == "__main__":
    check()
