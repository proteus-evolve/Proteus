"""Shared knowledge integrated with locked recipes, stage capabilities and recovery."""

from dataclasses import replace
import json
import subprocess
import sys

import pytest

from proteus.artifacts.codecs import FileContent
from proteus.core.methods import RunServices, StageContext
from proteus.core.serialization import ContractError
from proteus.evidence.records import EvidenceAccess
from proteus.examples.shared_knowledge import prepare, register
from proteus.knowledge.records import KnowledgeSnapshot
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.runner import run_recipe
from test_v1_evaluation import SOURCE
from test_v1_ledger import run_ledger


def setup(root):
    recipe = load_recipe(prepare(root / "study"))
    registry = builtin_registry()
    register(registry)
    recipe = registry.resolve(recipe)
    lock = registry.make_lock(recipe, [SOURCE])
    (root / "recipe.json").write_text(recipe.to_json())
    (root / "lock.json").write_text(lock.to_json())
    return recipe, registry, lock


def execute(root, recipe, registry, lock, *, resume=False):
    return run_recipe(recipe, registry=registry, lock=lock, root=root / "run", resume=resume,
                      artifact_source=root / "study/artifact-store")


def services(root, recipe, registry, lock):
    from proteus.core.serialization import content_hash
    return RunServices(run_id="run", recipe=recipe, registry=registry, root=root / "run", lock_hash=content_hash(lock))


def test_complete_recipe_records_only_released_evidence_and_retains_old_snapshots(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    assert any(pin.plugin.name == "proteus.file_tree" for pin in lock.plugins)
    result = execute(tmp_path, recipe, registry, lock)
    assert result.status.value == "completed", result.diagnostic
    knowledge = services(tmp_path, recipe, registry, lock).knowledge("research").store
    before = knowledge.get_snapshot(result.outputs["before"])
    after = knowledge.get_snapshot(result.outputs["after"])
    assert [(row.write.kind, row.revision) for row in knowledge.records(before)] == [("attempts", 1), ("notes", 1)]
    assert [(row.write.kind, row.revision) for row in knowledge.records(after)] == [("attempts", 1), ("notes", 2), ("skills", 1)]
    attempt = knowledge.records(after)[0]
    view = json.loads(knowledge.read(attempt.ref(), "view.json"))
    assert {row["evaluation_id"] for row in view["entries"]} == {None, "validation"}
    assert all(row["split"] in (None, "validation") for row in view["entries"])
    records = json.loads((tmp_path / "run/execution-0001.json").read_text())
    assert len(records["rollouts"]) == 3  # two released validation tasks and one hidden test
    events = run_ledger(tmp_path / "run").events()
    assert execute(tmp_path, recipe, registry, lock, resume=True) == result
    assert run_ledger(tmp_path / "run").events() == events


def test_configuration_cannot_disclose_hidden_evaluations_or_grant_unknown_roles(tmp_path):
    recipe, _, _ = setup(tmp_path)
    space = recipe.knowledge[0]
    with pytest.raises(ContractError, match="private evaluations"):
        replace(recipe, knowledge=(replace(space, evidence=EvidenceAccess(evaluation_ids=("test",), splits=("test",))),))
    with pytest.raises(ContractError, match="unknown role"):
        replace(recipe, knowledge=(replace(space, roles=("missing",)),))
    with pytest.raises(ContractError, match="unknown knowledge space"):
        replace(recipe, stages=(replace(recipe.stages[0], knowledge=("other",)),))


def test_stage_reads_require_declared_space_supplied_snapshot_and_included_revision(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    result = execute(tmp_path, recipe, registry, lock)
    assert result.status.value == "completed", result.diagnostic
    current = services(tmp_path, recipe, registry, lock)
    from proteus.core.contracts import StageRequest
    records = json.loads((tmp_path / "run/execution-0001.json").read_text())
    current.values.restore(records["values"])
    request = StageRequest.from_dict(records["requests"][-1])
    context = StageContext(current, recipe.stages[0], request)
    before, after = (current.values.get(result.outputs[key]) for key in ("before", "after"))
    with pytest.raises(ContractError, match="not supplied"):
        context.knowledge_entries(before)
    old_note = next(row for row in current.knowledge("research").store.records(before) if row.write.kind == "notes")
    with pytest.raises(ContractError, match="outside the supplied"):
        context.read_knowledge(after, old_note.ref(), "note.md")
    denied = StageContext(current, replace(recipe.stages[0], knowledge=()), request)
    with pytest.raises(ContractError, match="no access"):
        denied.knowledge_entries(after)
    with pytest.raises(ContractError, match="role cannot access"):
        current.knowledge("research").capture(role="unlisted", reader_id="intruder", operation_id="read")


WORKER = r"""
import os, sys
from pathlib import Path
from proteus.core.recovery import Journal
from proteus.examples.shared_knowledge import register
from proteus.knowledge.store import KnowledgeConflict, SharedKnowledgeStore
from proteus.recipes import load_recipe
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.manifest import DependencyLock
from proteus.recipes.runner import run_recipe
root, boundary = Path(sys.argv[1]), sys.argv[2]
registry = builtin_registry(); register(registry)
recipe = load_recipe(root / 'recipe.json')
lock = DependencyLock.from_json((root / 'lock.json').read_text())
if boundary == 'capture.receipt':
    original = SharedKnowledgeStore.snapshot
    def snapshot(self, **kwargs):
        result = original(self, **kwargs)
        if kwargs.get('operation_id') == 'read-a': os._exit(86)
        return result
    SharedKnowledgeStore.snapshot = snapshot
elif boundary in ('publish.receipt', 'conflict.receipt'):
    original = SharedKnowledgeStore._publish
    def publish(self, write):
        try:
            result = original(self, write)
        except KnowledgeConflict:
            if boundary == 'conflict.receipt' and write.operation_id == 'stale': os._exit(86)
            raise
        if boundary == 'publish.receipt' and write.operation_id == 'note-a': os._exit(86)
        return result
    SharedKnowledgeStore._publish = publish
else:
    original = Journal.call
    def call(self, kind, *args, **kwargs):
        result = original(self, kind, *args, **kwargs)
        if kind == boundary: os._exit(86)
        return result
    Journal.call = call
run_recipe(recipe, registry=registry, lock=lock, root=root/'run', artifact_source=root/'study/artifact-store')
"""


@pytest.mark.parametrize("boundary", ["capture.receipt", "knowledge.capture", "publish.receipt", "knowledge.attempt", "conflict.receipt"])
def test_hard_exit_preserves_recorded_reads_and_later_shared_publications(tmp_path, boundary):
    recipe, registry, lock = setup(tmp_path)
    stopped = subprocess.run([sys.executable, "-c", WORKER, str(tmp_path), boundary],
                             text=True, capture_output=True, timeout=45)
    assert stopped.returncode == 86, stopped.stdout + stopped.stderr
    knowledge = services(tmp_path, recipe, registry, lock).knowledge("research").store
    external = knowledge.publish("notes", "external", {"note.md": FileContent(b"Other worker's later discovery")},
        writer_id="worker-c", writer_role="proposer", operation_id="external")
    if boundary == "conflict.receipt":
        knowledge.publish("notes", "result", {"note.md": FileContent(b"A later shared update")},
            writer_id="worker-c", writer_role="proposer", operation_id="result-update", expected=knowledge.head("notes", "result"))
    result = execute(tmp_path, recipe, registry, lock, resume=True)
    assert result.status.value == "completed", result.diagnostic
    before = knowledge.get_snapshot(result.outputs["before"])
    after = knowledge.get_snapshot(result.outputs["after"])
    if boundary in ("capture.receipt", "knowledge.capture"):
        assert external not in before.revisions
    assert external in after.revisions
    assert len(knowledge.history("notes", "result")) == (3 if boundary == "conflict.receipt" else 2)
    assert len(knowledge.history("attempts", "initial")) == 1
    events = run_ledger(tmp_path / "run").events()
    assert execute(tmp_path, recipe, registry, lock, resume=True) == result
    assert run_ledger(tmp_path / "run").events() == events


def test_resume_rejects_missing_recorded_shared_snapshot(tmp_path):
    recipe, registry, lock = setup(tmp_path)
    result = execute(tmp_path, recipe, registry, lock)
    assert result.status.value == "completed", result.diagnostic
    knowledge = services(tmp_path, recipe, registry, lock).knowledge("research").store
    # Damage only the read evidence, leaving candidate/evaluation records intact.
    with knowledge._transaction(write=True) as db:
        db.execute("DELETE FROM reads")
        db.execute("DELETE FROM snapshots")
    with pytest.raises(ContractError, match="unknown knowledge snapshot"):
        execute(tmp_path, recipe, registry, lock, resume=True)


def test_snapshot_identity_is_part_of_the_configuration_and_dependency_lock(tmp_path):
    from proteus.recipes.manifest import RunManifest

    recipe, registry, lock = setup(tmp_path)
    altered = replace(recipe, knowledge=(replace(recipe.knowledge[0], id="renamed"),),
                      stages=(replace(recipe.stages[0], knowledge=("renamed",)),))
    assert recipe.config_hash != altered.config_hash
    with pytest.raises(ContractError):
        RunManifest.create(run_id="run", recipe=recipe, lock=lock).assert_resume_compatible(altered, lock)
    identity = services(tmp_path, recipe, registry, lock).knowledge("research").identity
    assert KnowledgeSnapshot(identity=identity, revisions=()).identity.space_id == "research"
