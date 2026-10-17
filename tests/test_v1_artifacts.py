"""Immutable candidates, replayable edits, corruption and real tree-loading checks."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from proteus.artifacts.codecs import (
    FileContent, FileDelta, FileWrite, RecordsDelta, Rename, StructuredRecords, TextComponents, TextDelta,
)
from proteus.artifacts.store import ArtifactManifest, ArtifactStore, CandidateDecision, publish_bytes
from proteus.core.contracts import ArtifactRef, Candidate, PluginRef, ValidationStatus
from proteus.core.serialization import ContractError, content_hash
from proteus.recipes.builtins import builtin_registry


TEXT = PluginRef(name="proteus.text_components", version="1")
RECORDS = PluginRef(name="proteus.structured_records", version="1")
FILES = PluginRef(name="proteus.file_tree", version="1")


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "objects", builtin_registry())


def seed(store):
    ref = store.put(TEXT, TextComponents(components={"system.txt": "original", "task.txt": "answer"}))
    candidate = Candidate(id="seed", run_id="study", artifact=ref, created_by="seed-operation",
                          created_attempt_id="attempt-1", validation=ValidationStatus.VALID)
    store.register_candidate(candidate)
    return candidate


def test_text_delta_replays_without_mutating_parent_or_callers(store):
    values = {"system.txt": "original", "task.txt": "answer"}
    original = store.put(TEXT, TextComponents(components=values))
    values["system.txt"] = "mutated outside the store"
    delta = TextDelta(base_hash=original.content_hash, put={"system.txt": "revised"}, delete=("task.txt",))
    revised = store.apply_delta(original, TextDelta.from_json(delta.to_json()))
    assert store.get(original).components == {"system.txt": "original", "task.txt": "answer"}
    assert store.get(revised).components == {"system.txt": "revised"}
    assert revised != original
    assert store.apply_delta(original, delta) == revised
    with pytest.raises(TypeError):
        store.get(original).components["system.txt"] = "mutated"
    with pytest.raises(ContractError, match="different base"):
        store.apply_delta(revised, delta)


def test_records_have_stable_ids_nested_values_and_deterministic_updates(store):
    records = {"skill/b": {"text": "x", "evidence": ["r1"]}, "skill a": {"count": 1}}
    original = store.put(RECORDS, StructuredRecords(records=records))
    assert store.put(RECORDS, StructuredRecords(records=dict(reversed(list(records.items()))))) == original
    changed = store.apply_delta(original, RecordsDelta(base_hash=original.content_hash,
        put={"skill a": {"count": 2, "text": "improved"}}, delete=("skill/b",)))
    assert store.get(changed).records == {"skill a": {"count": 2, "text": "improved"}}
    assert store.get(original).records["skill a"]["count"] == 1
    with pytest.raises(ContractError, match="missing"):
        store.apply_delta(original, RecordsDelta(base_hash=original.content_hash, delete=("unknown",)))
    with pytest.raises(ContractError, match="both deletes"):
        store.apply_delta(original, RecordsDelta(base_hash=original.content_hash,
            delete=("skill a",), put={"skill a": {"count": 2}}))
    with pytest.raises(ContractError, match="wrong codec"):
        store.apply_delta(original, TextDelta(base_hash=original.content_hash))


def test_binary_file_mode_identity_and_round_trip(store, tmp_path):
    files = {"binary.dat": FileContent(b"\xff\x00\x01"), "bin/run": FileContent(b"#!/bin/sh\n", True)}
    original = store.put(FILES, files)
    root = store.materialize(original, tmp_path / "materialized")
    assert (root / "binary.dat").read_bytes() == b"\xff\x00\x01"
    assert (root / "bin/run").stat().st_mode & 0o111
    assert store.capture_tree(root) == original
    different_mode = store.put(FILES, {**files, "bin/run": FileContent(b"#!/bin/sh\n")})
    assert original.content_hash != different_mode.content_hash
    (root / "binary.dat").write_bytes(b"writable trial")
    assert store.files(original)["binary.dat"].data == b"\xff\x00\x01"
    with pytest.raises(ContractError, match="differs"):
        store.verify_tree(original, root)
    with pytest.raises(ContractError, match="fresh destination"):
        store.materialize(different_mode, root)


def test_rename_delete_and_python_execution_use_exact_candidate(store, tmp_path):
    # Candidate A compiles/imports a module. Candidate B renames that module and changes
    # its result; a fresh load must neither execute A nor inherit A's compiled cache.
    original = store.put(FILES, {"main.py": FileContent(b"import old\nprint(old.answer)\n"),
                                "old.py": FileContent(b"answer = 'A'\n"),
                                "obsolete.txt": FileContent(b"remove")})
    root_a = store.materialize(original, tmp_path / "trial-a")
    assert subprocess.check_output([sys.executable, "main.py"], cwd=root_a, text=True).strip() == "A"
    assert list((root_a / "__pycache__").iterdir())
    renamed = store.apply_delta(original, FileDelta(base_hash=original.content_hash,
        rename=(Rename(source="old.py", destination="new.py"),), delete=("obsolete.txt",),
        put=(FileWrite.from_bytes("main.py", b"import new\nprint(new.answer)\n"),)))
    revised = store.apply_delta(renamed, FileDelta(base_hash=renamed.content_hash,
        put=(FileWrite.from_bytes("new.py", b"answer = 'B'\n"),)))
    root_b = store.materialize(revised, tmp_path / "trial-b")
    assert not (root_b / "__pycache__").exists()
    assert not (root_b / "old.py").exists()
    assert not (root_b / "obsolete.txt").exists()
    store.verify_tree(revised, root_b)
    assert subprocess.check_output([sys.executable, "main.py"], cwd=root_b, text=True).strip() == "B"
    root_a2 = store.materialize(original, tmp_path / "trial-a2")
    assert subprocess.check_output([sys.executable, "main.py"], cwd=root_a2, text=True).strip() == "A"


def test_simultaneous_rename_can_swap_files_and_rejects_ambiguous_edits(store):
    original = store.put(FILES, {"a": FileContent(b"A"), "b": FileContent(b"B")})
    swapped = store.apply_delta(original, FileDelta(base_hash=original.content_hash,
        rename=(Rename(source="a", destination="b"), Rename(source="b", destination="a"))))
    assert store.files(swapped) == {"a": FileContent(b"B"), "b": FileContent(b"A")}
    for delta in (
        FileDelta(base_hash=original.content_hash, rename=(Rename(source="a", destination="b"),)),
        FileDelta(base_hash=original.content_hash, rename=(Rename(source="missing", destination="c"),)),
        FileDelta(base_hash=original.content_hash, rename=(Rename(source="a", destination="c"),), delete=("a",)),
        FileDelta(base_hash=original.content_hash, rename=(Rename(source="a", destination="c"),),
                  put=(FileWrite.from_bytes("c", b"bad"),)),
        FileDelta(base_hash=original.content_hash, rename=(Rename(source="a", destination="c"),),
                  put=(FileWrite.from_bytes("a", b"bad"),)),
    ):
        with pytest.raises(ContractError):
            store.apply_delta(original, delta)
    assert store.files(original) == {"a": FileContent(b"A"), "b": FileContent(b"B")}


@pytest.mark.parametrize("paths", [
    ["../escape"], ["/absolute"], ["a\\b"], ["a/./b"], ["a//b"], ["C:drive"],
    ["a", "a/b"], ["Foo/x", "foo/y"], ["A", "a"], ["é", "e\u0301"],
    ["con.txt"], ["trailing."], ["question?"], ["nul/b"], ["bad\x00file"],
])
def test_nonportable_or_ambiguous_trees_fail_before_publication(store, paths):
    with pytest.raises(ContractError):
        store.put(FILES, {path: FileContent(b"value") for path in paths})
    assert not store.root.exists()


def test_tree_capture_rejects_links_and_special_files(store, tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "real").write_text("a")
    (root / "link").symlink_to(root / "real")
    with pytest.raises(ContractError, match="regular files"):
        store.capture_tree(root)
    (root / "link").unlink()
    (root / "loop").symlink_to(root, target_is_directory=True)
    with pytest.raises(ContractError, match="symlinks"):
        store.capture_tree(root)
    (root / "loop").unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "pipe")
        with pytest.raises(ContractError, match="regular files"):
            store.capture_tree(root)


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="requires POSIX directory permissions enforced for a non-root user")
def test_capture_does_not_silently_omit_unreadable_subtrees(store, tmp_path):
    root = tmp_path / "tree"
    private = root / "unreadable"
    private.mkdir(parents=True)
    (private / "must-not-disappear").write_text("source")
    private.chmod(0)
    try:
        with pytest.raises(PermissionError):
            store.capture_tree(root)
        assert not store.root.exists()
    finally:
        private.chmod(0o755)


def test_fork_failed_candidate_and_native_import_are_retained_across_reopen(store):
    parent = seed(store)
    child = store.fork(parent, candidate_id="attempt/with arbitrary ID", created_by="proposal",
        created_attempt_id="attempt-1", validation=ValidationStatus.INVALID,
        delta=TextDelta(base_hash=parent.artifact.content_hash, put={"system.txt": "failed proposal"}))
    decision = CandidateDecision(run_id="study", candidate_id=child.id, operation_id="validation",
        attempt_id="attempt-1", disposition="failed", reason="invalid candidate; retain for repair")
    assert store.decide(decision) == store.decide(decision)
    reopened = ArtifactStore(store.root, builtin_registry())
    assert reopened.candidate("study", child.id) == child
    assert reopened.decisions("study", child.id) == (decision,)
    assert reopened.candidate("study", "seed") == parent
    repaired = reopened.fork(child, candidate_id="repaired", created_by="repair", created_attempt_id="a2",
        delta=TextDelta(base_hash=child.artifact.content_hash, put={"system.txt": "corrected"}))
    assert repaired.parent_ids == (child.id,)
    assert reopened.get(child.artifact).components["system.txt"] == "failed proposal"
    imported = replace(parent, id="native")
    store.register_candidate(imported, source_checkpoint="git:abc123")
    entry = store.candidate_entry("study", "native")
    assert entry.origin == "native_checkpoint_import" and entry.source_checkpoint == "git:abc123"
    assert store.candidate_entry("study", "seed").origin == "recipe"


def test_candidate_identity_cannot_be_reused_for_new_artifact_or_parents(store):
    parent = seed(store)
    different = store.put(TEXT, TextComponents(components={"system.txt": "new"}))
    with pytest.raises(ContractError, match="different content"):
        store.register_candidate(replace(parent, artifact=different))
    with pytest.raises(FileNotFoundError):
        store.register_candidate(replace(parent, id="child", parent_ids=("missing",)))
    with pytest.raises(FileNotFoundError):
        store.register_candidate(replace(parent, id="child", run_id="other", parent_ids=("seed",)))
    assert store.candidate("study", "seed") == parent


def test_manifest_runtime_and_environment_are_part_of_identity(store):
    value = TextComponents(components={"prompt": "x"})
    base = store.put(TEXT, value)
    bound = store.put(TEXT, value, runtime=PluginRef(name="runner", version="2"),
                      environment_hash=content_hash("environment"))
    assert base.content_hash != bound.content_hash
    with pytest.raises(ContractError, match="differs from stored"):
        store.validate(replace(bound, runtime=PluginRef(name="different", version="2")))
    changed = store.apply_delta(bound, TextDelta(base_hash=bound.content_hash, put={"prompt": "y"}))
    assert changed.runtime == bound.runtime and changed.environment_hash == bound.environment_hash
    assert ArtifactManifest.from_json(store.manifest(bound).to_json()).ref() == bound
    assert ArtifactRef.from_json(bound.to_json()) == bound


def test_blob_and_manifest_corruption_are_detected_before_materialization(store, tmp_path):
    original = store.put(FILES, {"data": FileContent(b"original")})
    blob = next((store.root / "blobs").iterdir())
    blob.chmod(0o644)
    blob.write_bytes(b"modified")
    with pytest.raises(ContractError, match="corrupt"):
        store.materialize(original, tmp_path / "never-created")
    assert not (tmp_path / "never-created").exists()
    with pytest.raises(ContractError, match="different content"):
        store.put(FILES, {"data": FileContent(b"original")})


def test_candidate_entry_corruption_is_detected(store):
    parent = seed(store)
    entry = next((store.root / "candidate-entries").iterdir())
    data = json.loads(entry.read_text())
    data["candidate"]["created_by"] = "tampered"
    entry.chmod(0o644)
    entry.write_text(json.dumps(data))
    with pytest.raises(ContractError, match="corrupt"):
        store.candidate(parent.run_id, parent.id)


def test_copy_between_stores_verifies_all_bytes(store, tmp_path):
    original = store.put(RECORDS, StructuredRecords(records={"one": {"text": "hello"}}))
    other = ArtifactStore(tmp_path / "other", builtin_registry())
    other.copy_from(store, original)
    assert other.get(original) == store.get(original)
    assert other.manifest(original) == store.manifest(original)


def test_parallel_identical_publications_and_conflicting_writers(tmp_path):
    path = tmp_path / "immutable"
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: publish_bytes(path, b"same"), range(16)))
    assert path.read_bytes() == b"same"
    assert not list(tmp_path.glob(".pending-*"))
    with pytest.raises(ContractError, match="different content"):
        publish_bytes(path, b"different")
    assert path.read_bytes() == b"same"


def test_interrupted_manifest_publication_can_retry_without_partial_reference(store, monkeypatch):
    from pathlib import Path
    original_link = os.link

    def fail_manifest(source, destination):
        if Path(destination).parent.name == "manifests":
            raise OSError("injected publication failure")
        original_link(source, destination)

    value = TextComponents(components={"system": "a", "task": "b"})
    with monkeypatch.context() as patch:
        patch.setattr(os, "link", fail_manifest)
        with pytest.raises(OSError, match="injected"):
            store.put(TEXT, value)
    assert list((store.root / "blobs").iterdir())  # complete but unreferenced bytes are harmless
    assert not list((store.root / "manifests").iterdir())
    assert not list(store.root.rglob(".pending-*"))
    reference = store.put(TEXT, value)
    assert store.get(reference) == value


def test_symlink_cannot_impersonate_blob_or_existing_publication(store, tmp_path):
    original = store.put(FILES, {"data": FileContent(b"original")})
    blob = next((store.root / "blobs").iterdir())
    real = tmp_path / "real"
    real.write_bytes(blob.read_bytes())
    blob.unlink()
    blob.symlink_to(real)
    with pytest.raises(ContractError, match="regular stored file"):
        store.validate(original)
    with pytest.raises(ContractError, match="regular stored file"):
        store.put(FILES, {"data": FileContent(b"original")})


@pytest.mark.parametrize("value", ["!!!", "YQ", "YR==", "YQ==\n", "☃"])
def test_file_delta_rejects_invalid_or_noncanonical_base64(value):
    with pytest.raises(ContractError):
        FileWrite(path="file", base64=value)


def test_method_services_import_fork_record_failure_and_export_lineage(store, tmp_path):
    from proteus.core.contracts import OperationStatus, StageRequest, StageResult, validate_records
    from proteus.core.methods import MethodResult
    from proteus.core.plugins import EmptyOptions, Plugin
    from proteus.recipes.config import PluginSpec, RecipeConfig, ReferenceSpec, SubjectSpec
    from proteus.recipes.manifest import SourcePin
    from proteus.recipes.runner import run_recipe

    registry = builtin_registry()
    initial = store.put(TEXT, TextComponents(components={"system": "initial"}))

    class ForkStudy:
        def run(self, services, restored_state):
            parent = services.initial_candidate()
            child = services.fork_candidate(parent, candidate_id="child", delta=TextDelta(
                base_hash=parent.artifact.content_hash, put={"system": "changed"}))
            # A method can inspect a failed proposal and continue. Its failed operation,
            # original delta and unmodified parent must remain in the experiment output.
            with pytest.raises(ContractError, match="different base"):
                services.fork_candidate(child, delta=TextDelta(base_hash=parent.artifact.content_hash))
            services.artifacts.decide(CandidateDecision(run_id=services.run_id, candidate_id=child.id,
                operation_id="selection", attempt_id="attempt-1", disposition="rejected",
                reason="fixture decision; candidate remains inspectable"))
            return MethodResult(outputs={"child": services.values.publish("proteus.candidate@1", child)})

    for name, kind, factory in (("study.fork", "method", lambda options: ForkStudy()),
                                ("study.subject", "subject", lambda options: object())):
        registry.register(Plugin(ref=PluginRef(name=name, version="1"), kind=kind, options=EmptyOptions,
            factory=factory, source="proteus", implementation_hash=content_hash("fixture")))
    recipe = RecipeConfig(name="fork-study", method=PluginSpec(plugin=PluginRef(name="study.fork", version="1")),
        actors={}, reference=ReferenceSpec(profile="native"),
        subject=SubjectSpec(runner=PluginSpec(plugin=PluginRef(name="study.subject", version="1")),
                            initial_artifact=initial))
    lock = registry.make_lock(recipe, [SourcePin(name="proteus", repository="fixture", revision="0" * 40)])
    root = tmp_path / "run"
    result = run_recipe(recipe, registry=registry, lock=lock, root=root, artifact_source=store.root)
    assert result.status == OperationStatus.COMPLETED, result.diagnostic
    records = json.loads((root / "execution-0001.json").read_text())
    candidates = [Candidate.from_dict(item) for item in records["candidates"]]
    requests = [StageRequest.from_dict(item) for item in records["requests"]]
    results = [StageResult.from_dict(item) for item in records["results"]]
    validate_records(run_id="run", candidates=candidates, requests=requests, results=results)
    assert len(candidates) == 2 and len(requests) == 3
    assert results[-1].status == OperationStatus.FAILED
    assert "delta" in requests[-1].inputs
    reloaded = ArtifactStore(root / "artifacts", registry)
    assert reloaded.get(candidates[0].artifact).components["system"] == "initial"
    assert reloaded.get(candidates[1].artifact).components["system"] == "changed"
    assert reloaded.decisions("run", "child")[0].disposition == "rejected"


def test_run_store_rejects_undeclared_codecs_before_writing(store, tmp_path):
    limited = ArtifactStore(tmp_path / "limited", builtin_registry(), allowed_codecs=(TEXT,))
    with pytest.raises(ContractError, match="not declared"):
        limited.put(RECORDS, StructuredRecords(records={}))
    assert not limited.root.exists()
