"""Prepare a local, deterministic evidence-view demonstration, not a method benchmark."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from proteus.core.contracts import PluginRef, require
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import canonical_json
from proteus.evidence.records import EvidenceAccess
from proteus.examples.candidate_evaluation import plugin, prepare as prepare_comparison
from proteus.methods.compare import CompareOptions
from proteus.recipes import load_recipe
from proteus.recipes.builtins import Text
from proteus.recipes.config import ActorSpec, StageSpec


class InspectHistory:
    def run(self, context, inputs):
        view = inputs["history"]
        code = {entry.candidate_id: context.read_evidence(view, entry.path).decode()
                for entry in view.entries if entry.kind == "artifact" and entry.path.endswith("/main.py")}
        matches = context.search_evidence(view, "output")
        return {"inspection": Text(text=canonical_json({"candidates": view.candidate_ids,
            "code": code, "search_matches": matches, "files": len(view.entries)}))}


class HistoryMethod:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        # No explicit checkpoint: the serial operation journal replays committed calls.
        initial = services.initial_candidate()
        codec = services.artifacts._codec(initial.artifact.codec)
        candidates = [initial, *(services.fork_candidate(initial, candidate_id=variant.id,
            delta=codec.delta_schema.from_dict(variant.delta)) for variant in self.options.variants)]
        for candidate in candidates:
            for evaluation in self.options.evaluations:
                services.evaluate(candidate, evaluation)
        history = services.evidence_view("inspect")
        result = services.stage("inspect", {"history": history})
        return MethodResult(outputs={"history": history, "inspection": result.outputs["inspection"]})


def validate(recipe, options):
    require(recipe.subject is not None and recipe.execution is not None, "history example needs executable candidates")
    require(set(options.evaluations) <= {spec.id for spec in recipe.evaluations}, "unknown history evaluation")
    require(len(recipe.stages) == 1 and recipe.stages[0].id == "inspect" and not recipe.search and recipe.tasks is None,
            "history example uses one inspect stage and declared evaluations")


def register(registry):
    digest = implementation_hash({"evidence_history.py": Path(__file__)})
    for name, kind, options, factory, kwargs in (
        ("history", "method", CompareOptions, HistoryMethod,
         {"capabilities": ("run.journal", "run.resume"), "validate": validate, "state_schema": EmptyOptions}),
        ("inspect", "stage", EmptyOptions, lambda options: InspectHistory(),
         {"capabilities": ("stage.replay",), "inputs": {"history": "proteus.evidence.view@1"},
          "outputs": {"inspection": "proteus.text@1"}}),
    ):
        registry.register(Plugin(ref=PluginRef(name=f"example.evidence_{name}", version="1"), kind=kind,
            options=options, factory=factory, implementation_hash=digest, source="proteus", **kwargs))


def prepare(root: Path) -> Path:
    path = prepare_comparison(root)
    recipe = load_recipe(path)
    stage = StageSpec(id="inspect", role="proposer", operator=plugin("example.evidence_inspect"),
        input_types={"history": "proteus.evidence.view@1"}, output_types={"inspection": "proteus.text@1"},
        read_scopes=("proteus.evidence.view@1",), write_scopes=("proteus.text@1",),
        evidence=EvidenceAccess(evaluation_ids=("validation",), splits=("validation",)))
    recipe = replace(recipe, name="offline-evidence-history",
        method=plugin("example.evidence_history", **dict(recipe.method.options)), stages=(stage,),
        actors={"proposer": ActorSpec(runtime=plugin("proteus.echo"))})
    path.write_text(recipe.to_json() + "\n", encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    print(prepare(parser.parse_args().out))


if __name__ == "__main__":
    main()
