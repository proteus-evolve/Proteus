"""ACE reference edge cases, immutable artifacts and hard-exit recovery."""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace

import pytest

from proteus.artifacts.codecs import FileContent
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import BudgetLimits, OperationStatus, PluginRef
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin
from proteus.core.serialization import Contract, ContractError, canonical_json, content_hash
from proteus.methods.ace.playbook import CODEC, EMPTY_PLAYBOOK, REPOSITORY, REVISION, Playbook, PlaybookCodec, PlaybookDelta, register
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import RecipeConfig, ReferenceSpec, SubjectSpec
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE, plugin


def store_at(root):
    registry = builtin_registry()
    register(registry)
    return ArtifactStore(root, registry)


def delta(base, kind, *records):
    return PlaybookDelta(base_hash=base.content_hash, kind=kind, records=records)


def test_real_pinned_reference():
    source = os.environ.get("PROTEUS_ACE_SOURCE")
    if not source:
        pytest.skip("set PROTEUS_ACE_SOURCE to the pinned ACE checkout; mandatory in CI")
    from check_ace_reference import check
    report = check(source)
    assert report["functions"] == 8 and report["updates"] == 768 and report["extractions"] == 2304


def test_reference_counter_duplicates_alias_whitespace_and_ignored_tags(tmp_path):
    store = store_at(tmp_path)
    text = "\n## OTHERS\n  [misc-00001] helpful=06 harmful=01 :: Keep this.  \n  continuation\n\n"
    base = store.put(CODEC, Playbook.from_text(text, next_id=40))
    empty = store.apply_delta(base, delta(base, "counters", None, {}, {"id": ""}))
    assert empty == base
    changed = store.apply_delta(base, delta(base, "counters", {"id": "misc-00001", "tag": "helpful"},
        {"bullet": "misc-00001", "tag": "harmful"}))
    value = store.get(changed)
    assert value.text == "## OTHERS\n[misc-00001] helpful=6 harmful=2 :: Keep this.\n  continuation"
    assert value.next_id == 40
    assert value.bullets[0].helpful == 6 and value.bullets[0].harmful == 2
    assert store.get(base).text == text
    # A nonempty unknown ID still activates upstream's outer strip; neutral tags
    # normalize matching bullet formatting. Neither behavior is silently fixed.
    unknown = store.apply_delta(base, delta(base, "counters", {"id": "missing", "tag": "helpful"}))
    assert store.get(unknown).text == text.strip()
    neutral = store.apply_delta(base, delta(base, "counters", {"id": "misc-00001", "tag": "unsupported"}))
    assert "helpful=6 harmful=1" in store.get(neutral).text


def test_add_order_section_slugs_multiline_and_unsupported_operations(tmp_path):
    store = store_at(tmp_path)
    base = store.put(CODEC, Playbook.from_text())
    proposed = delta(base, "curate",
        {"type": "ADD", "section": "STRATEGIES & INSIGHTS", "content": "a\n  continuation"},
        {"type": "UPDATE", "bullet_id": "sai-00001", "content": "replacement"},
        {"type": "ADD", "section": "FORMULAS & CALCULATIONS", "content": "b"},
        {"type": "ADD", "section": "missing", "content": "c"},
        {"type": "ADD", "content": "d"},  # default general falls immediately after OTHERS header
        {"type": "DELETE", "bullet_id": "sai-00001"})
    assert proposed.ignored_operations == (1, 5)
    ref = store.apply_delta(base, proposed)
    value = store.get(ref)
    assert [b.id for b in value.bullets] == ["sai-00001", "calc-00002", "gene-00004", "misc-00003"]
    assert "a\n  continuation" in value.text and "replacement" not in value.text
    assert value.next_id == 5 and value.stats()["total_bullets"] == 4
    assert value.extract(["misc-00003", "sai-00001"]) == (
        "[sai-00001] helpful=0 harmful=0 :: a\n[misc-00003] helpful=0 harmful=0 :: c")
    assert store.apply_delta(base, PlaybookDelta.from_json(proposed.to_json())) == ref
    assert store.get(base) == Playbook.from_text()
    with pytest.raises(ContractError, match="different base"):
        store.apply_delta(ref, proposed)


def test_index_round_trip_order_counter_state_and_tampering(tmp_path):
    store = store_at(tmp_path)
    value = Playbook.from_text("\n## OTHERS\n[z-00002] helpful=2 harmful=0 :: z\n[a-00001] helpful=0 harmful=0 :: a\n", next_id=9)
    original = store.put(CODEC, value)
    reloaded = ArtifactStore(tmp_path, store.registry).get(original)
    assert reloaded == value and [b.id for b in reloaded.bullets] == ["z-00002", "a-00001"]
    assert reloaded.bullets[0].line == 3
    assert store.put(CODEC, replace(value, next_id=10)) != original
    files = dict(store.files(original))
    index = json.loads(files["index.json"].data)
    for mutated in (dict(index, bullets=index["bullets"][::-1]), dict(index, source_revision="wrong"),
                    dict(index, schema_version=2), dict(index, next_id=1)):
        with pytest.raises(ContractError):
            PlaybookCodec().decode({**files, "index.json": FileContent(canonical_json(mutated).encode())})
    with pytest.raises(ContractError, match="canonical"):
        PlaybookCodec().decode({**files, "index.json": FileContent(json.dumps(index, indent=2).encode())})
    with pytest.raises(ContractError, match="non-executable"):
        PlaybookCodec().decode({**files, "playbook.txt": FileContent(value.text.encode(), True)})
    destination = store.materialize(original, tmp_path / "published")
    assert (destination / "playbook.txt").read_bytes() == value.text.encode()
    (destination / "playbook.txt").write_text("changed externally")
    assert store.get(original) == value


@pytest.mark.parametrize("text,next_id", [
    ("text before a header\n## OTHERS", 1), ("", 1),
    ("## OTHERS\n[a-00003] helpful=0 harmful=0 :: x", 3),
    ("## OTHERS\n[x] helpful=0 harmful=0 :: a\n[x] helpful=1 harmful=0 :: b", 1),
])
def test_ambiguous_or_non_curatable_state_is_rejected(text, next_id):
    with pytest.raises(ContractError):
        Playbook(text=text, next_id=next_id)


def test_failed_update_does_not_publish_or_mutate_parent(tmp_path):
    store = store_at(tmp_path)
    base = store.put(CODEC, Playbook.from_text("## A  B\n## OTHERS"))
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file())
    with pytest.raises(ContractError, match="reference rejected"):
        store.apply_delta(base, delta(base, "curate", {"type": "ADD", "section": "A  B", "content": "x"}))
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file()) == before
    assert store.get(base).next_id == 1
    with pytest.raises(ContractError, match="reuse"):
        store.apply_delta(base, delta(base, "curate", {"type": "ADD", "section": "OTHERS",
            "content": "first\n[other-00999] helpful=0 harmful=0 :: injected ID"}))
    assert store.get(base).next_id == 1
    for kind, records in (("curate", [None]), ("curate", [{"type": "ADD", "content": 3}]),
                          ("counters", [{"id": ["not hashable"]}])):
        with pytest.raises(ContractError):
            delta(base, kind, *records)


@dataclass(frozen=True, kw_only=True)
class FixtureState(Contract):
    ids: tuple[str, ...]


class FixtureMethod:
    """Exercises public services with fixed deltas; never labeled ACE training."""

    def run(self, services, restored_state):
        if restored_state is None:
            state = FixtureState(ids=(services.initial_candidate().id,))
            checkpoint = services.checkpoint(state)
        else:
            state, checkpoint = FixtureState.from_dict(restored_state.state.payload), restored_state
        steps = (("curate", ({"type": "ADD", "section": "OTHERS", "content": "first"},
                             {"type": "UPDATE", "bullet_id": "misc-00001", "content": "ignored"})),
                 ("counters", ({"id": "misc-00001", "tag": "helpful"},)),
                 ("curate", ({"type": "ADD", "section": "OTHERS", "content": "second"},)))
        for i in range(len(state.ids) - 1, len(steps)):
            parent = next(c for c in services.candidates if c.id == state.ids[-1])
            kind, records = steps[i]
            candidate = services.fork_candidate(parent, candidate_id=f"update-{i + 1}",
                delta=delta(parent.artifact, kind, *records))
            state = FixtureState(ids=(*state.ids, candidate.id))
            checkpoint = services.checkpoint(state)
        final = next(c for c in services.candidates if c.id == state.ids[-1])
        value = services.artifacts.get(final.artifact)
        return MethodResult(outputs={"playbook": services.values.publish("proteus.ace.playbook@1", value)}, state=checkpoint)


def fixture_setup(root):
    store = store_at(root / "source")
    for name, kind, factory, schema, capabilities in (
        ("example.ace_updates", "method", lambda options: FixtureMethod(), FixtureState, ("run.resume", "run.journal")),
        ("example.unused_subject", "subject", lambda options: None, None, ()),
    ):
        store.registry.register(Plugin(ref=PluginRef(name=name, version="1"), kind=kind,
            options=EmptyOptions, factory=factory, source="proteus", implementation_hash=content_hash(name),
            state_schema=schema, capabilities=capabilities))
    initial = store.put(CODEC, Playbook.from_text())
    recipe = RecipeConfig(name="ace-playbook-delta-fixture", method=plugin("example.ace_updates"),
        actors={}, reference=ReferenceSpec(profile="extended", code=REPOSITORY, revision=REVISION,
            deviations=("Fixed deltas; no ACE training or model.",)),
        subject=SubjectSpec(runner=plugin("example.unused_subject"), initial_artifact=initial),
        budget=BudgetLimits(max_model_calls=0, max_evaluations=0))
    return store, recipe


WORKER = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_v1_ace_playbook import fixture_setup, PlaybookCodec, SOURCE
from proteus.artifacts.store import ArtifactStore
from proteus.core.recovery import Journal
from proteus.recipes.runner import run_recipe
root, boundary, resume = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == 'resume'
store, recipe = fixture_setup(root)
apply = PlaybookCodec.apply
def observed(self, value, delta):
    with (root/'updates.txt').open('a') as stream: stream.write(delta.kind+'\n')
    return apply(self, value, delta)
PlaybookCodec.apply = observed
if not resume:
    publish = ArtifactStore.register_candidate
    def registered(self, candidate, *args, **kwargs):
        result = publish(self, candidate, *args, **kwargs)
        if boundary == 'registered' and candidate.id == 'update-2': os._exit(73)
        return result
    ArtifactStore.register_candidate = registered
    call = Journal.call
    def committed(self, kind, *args, **kwargs):
        result = call(self, kind, *args, **kwargs)
        if boundary == 'committed' and kind == 'candidate' and getattr(result, 'id', None) == 'update-2': os._exit(73)
        return result
    Journal.call = committed
lock = store.registry.make_lock(recipe, [SOURCE])
result = run_recipe(recipe, registry=store.registry, lock=lock, root=root/'run', resume=resume, artifact_source=store.root)
print(result.to_json())
'''


@pytest.mark.parametrize("boundary", ["registered", "committed"])
def test_hard_exit_preserves_ids_counter_updates_and_candidate_lineage(tmp_path, boundary):
    def invoke(resume):
        return subprocess.run([sys.executable, "-c", WORKER, str(tmp_path), boundary, "resume" if resume else "run"],
            capture_output=True, text=True, timeout=30)
    interrupted = invoke(False)
    assert interrupted.returncode == 73, interrupted.stderr
    resumed = invoke(True)
    assert resumed.returncode == 0, resumed.stderr
    result = MethodResult.from_json(resumed.stdout)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    again = invoke(True)
    assert again.returncode == 0 and MethodResult.from_json(again.stdout) == result, again.stderr
    exported = json.loads(sorted((tmp_path / "run").glob("execution-*.json"))[-1].read_text())
    candidates = exported["candidates"]
    assert [c["id"] for c in candidates] == ["initial", "update-1", "update-2", "update-3"]
    assert [c["parent_ids"] for c in candidates] == [[], ["initial"], ["update-1"], ["update-2"]]
    final = next(item["value"] for item in exported["values"] if item["ref"]["kind"] == "proteus.ace.playbook@1")
    value = Playbook.from_dict(final)
    assert value.next_id == 3 and [(b.id, b.helpful, b.harmful) for b in value.bullets] == [
        ("misc-00001", 1, 0), ("misc-00002", 0, 0)]
    assert exported["actor_calls"] == [] and exported["rollouts"] == []
    assert (tmp_path / "updates.txt").read_text().splitlines() == (
        ["curate", "counters", "counters", "curate"] if boundary == "registered"
        else ["curate", "counters", "curate"])


def test_delta_events_keep_ignored_raw_records_and_text_has_no_forced_rewrite(tmp_path):
    store, recipe = fixture_setup(tmp_path)
    result = run_recipe(recipe, registry=store.registry, lock=store.registry.make_lock(recipe, [SOURCE]),
        root=tmp_path / "run", artifact_source=store.root)
    assert result.status == OperationStatus.COMPLETED
    exported = json.loads((tmp_path / "run" / "execution-0001.json").read_text())
    proposals = [v["value"]["delta"] for v in exported["values"] if v["ref"]["kind"] == "proteus.artifact_proposal@1"]
    assert [p["kind"] for p in proposals] == ["curate", "counters", "curate"]
    assert proposals[0]["records"][1] == {"type": "UPDATE", "bullet_id": "misc-00001", "content": "ignored"}
    assert len({p["base_hash"] for p in proposals}) == 3
    assert store.get(recipe.subject.initial_artifact).text == EMPTY_PLAYBOOK
