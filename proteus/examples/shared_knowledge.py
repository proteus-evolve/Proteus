"""Offline recipe demonstrating recorded research knowledge and bounded stage reads."""

import argparse
from dataclasses import replace
from pathlib import Path

from proteus.artifacts.codecs import FileContent
from proteus.core.contracts import PluginRef, require
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import canonical_json
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.evidence_history import prepare as prepare_history
from proteus.knowledge.records import SNAPSHOT_KIND
from proteus.recipes import load_recipe
from proteus.recipes.builtins import Text
from proteus.recipes.config import KnowledgeSpec


class InspectKnowledge:
    def run(self, context, inputs):
        snapshot = inputs["knowledge"]
        entries, text = [], {}
        for record in context.knowledge_entries(snapshot):
            write = record.write
            entries.append({"kind": write.kind, "key": write.key, "writer_id": write.writer_id,
                            "writer_role": write.writer_role, "revision": record.revision})
            if write.artifact is not None and write.kind in ("notes", "skills"):
                for item in write.artifact.components:
                    text[f"{write.kind}/{write.key}/{item.path}"] = context.read_knowledge(
                        snapshot, record.ref(), item.path).decode()
        return {"inspection": Text(text=canonical_json({"entries": entries, "text": text}))}


class KnowledgeMethod:
    def run(self, services, restored_state):
        initial = services.initial_candidate()
        services.evaluate(initial, "validation")
        services.evaluate(initial, "test")  # Hidden results never enter the shared space.
        history = services.evidence_view("inspect", candidate_ids=(initial.id,))
        knowledge = services.knowledge("research")
        attempt = knowledge.record_attempt("initial", history, role="proposer", writer_id="worker-a", operation_id="evaluate")
        require(attempt.status == "published", "expected a new attempt")
        first = knowledge.publish("notes", "result", {"note.md": FileContent(b"Inspect the released validation evidence.")},
            role="proposer", writer_id="worker-a", operation_id="note-a", sources=(attempt.revision,))
        require(first.status == "published", "expected first note")
        before = knowledge.capture(role="proposer", reader_id="worker-a", operation_id="read-a")
        second = knowledge.publish("notes", "result", {"note.md": FileContent(b"Worker B reviewed the earlier result.")},
            role="proposer", writer_id="worker-b", operation_id="note-b", expected=first.revision, sources=(first.revision,))
        require(second.status == "published", "expected revised note")
        stale = knowledge.publish("notes", "result", {"note.md": FileContent(b"Stale edit must not overwrite B.")},
            role="proposer", writer_id="worker-a", operation_id="stale", expected=first.revision)
        require(stale.status == "conflict" and stale.current == second.revision, "stale write must report the observed conflict")
        skill = knowledge.publish("skills", "validation", {"SKILL.md": FileContent(b"# Validate\nRead the released evaluation record.")},
            role="proposer", writer_id="worker-b", operation_id="skill", sources=(second.revision, attempt.revision))
        require(skill.status == "published", "expected new skill")
        after = knowledge.capture(role="proposer", reader_id="worker-b", operation_id="read-b")
        inspection = services.stage("inspect", {"history": history, "knowledge": after})
        return MethodResult(outputs={"before": before, "after": after, "inspection": inspection.outputs["inspection"]})


def register(registry):
    digest = implementation_hash({"shared_knowledge.py": Path(__file__)})
    registry.register(Plugin(ref=PluginRef(name="example.shared_knowledge", version="1"), kind="method",
        options=EmptyOptions, factory=lambda options: KnowledgeMethod(), implementation_hash=digest, source="proteus",
        capabilities=("run.journal", "run.resume"), state_schema=EmptyOptions))
    registry.register(Plugin(ref=PluginRef(name="example.inspect_knowledge", version="1"), kind="stage",
        options=EmptyOptions, factory=lambda options: InspectKnowledge(), implementation_hash=digest, source="proteus",
        capabilities=("stage.replay",), inputs={"history": "proteus.evidence.view@1", "knowledge": SNAPSHOT_KIND},
        outputs={"inspection": "proteus.text@1"}))


def prepare(root):
    path = prepare_history(root)
    recipe = load_recipe(path)
    stage = replace(recipe.stages[0], operator=plugin("example.inspect_knowledge"), knowledge=("research",),
        input_types={"history": "proteus.evidence.view@1", "knowledge": SNAPSHOT_KIND},
        read_scopes=("proteus.evidence.view@1", SNAPSHOT_KIND))
    recipe = replace(recipe, name="offline-shared-knowledge", method=plugin("example.shared_knowledge"), stages=(stage,),
                     knowledge=(KnowledgeSpec(id="research", roles=("proposer",), evidence=stage.evidence),))
    path.write_text(recipe.to_json() + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == "__main__":
    main()
