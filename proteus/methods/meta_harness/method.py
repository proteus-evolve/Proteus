"""Meta-Harness search over explicit code artifacts and journaled memory learning."""

import json
from pathlib import Path

from proteus.artifacts.codecs import FileContent
from proteus.core.contracts import (
    FeedbackVisibility,
    OperationStatus,
    Outcome,
    PluginRef,
    StatePolicy,
    require,
)
from proteus.core.methods import MethodResult
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.serialization import canonical_json, read_json
from proteus.execution.code_records import CodeCall, CodeOptions, CodeState
from proteus.evaluation.tasks import make_batch
from proteus.methods.meta_harness.benchmark import benchmark, evaluate
from proteus.methods.meta_harness.records import (
    REPOSITORY,
    REVISION,
    BenchmarkResult,
    IterationResult,
    MetaOptions,
    MetaSummary,
    final_systems,
    frontier,
)
from proteus.recipes.builtins import Text


def validate(recipe, options, registry):
    require(
        recipe.reference.code == REPOSITORY and recipe.reference.revision == REVISION,
        "Meta-Harness requires the pinned reference identity",
    )
    require(
        recipe.reference.profile in ("adapted", "extended") and recipe.reference.deviations,
        "managed Meta-Harness requires declared runtime and process-state adaptations",
    )
    require(
        recipe.subject is not None and recipe.subject.runner.plugin.name == "proteus.code_subject",
        "Meta-Harness requires the managed candidate-code subject",
    )
    code = CodeOptions.from_dict(recipe.subject.runner.options)
    require(
        code.module == "candidate"
        and code.class_name == "Candidate"
        and set(code.methods) == {"inspect", "predict", "learn_from_batch"}
        and code.prediction_output == "answer_metadata"
        and code.prediction_method == "predict"
        and code.state_getter == "get_state"
        and code.state_setter == "set_state",
        "Meta-Harness candidate bridge differs",
    )
    require(
        recipe.execution is not None and recipe.execution.state_policy == StatePolicy.SEQUENCE,
        "Meta-Harness serial profile carries explicit memory between predictions",
    )
    require(
        recipe.tasks is None and not recipe.search,
        "Meta-Harness owns its declared pools and frontier",
    )
    stages = {s.id: s for s in recipe.stages}
    require(
        set(stages) == {options.proposer_stage},
        "Meta-Harness requires its configured proposer stage",
    )
    stage = stages[options.proposer_stage]
    require(
        dict(stage.input_types)
        == {
            "prompt": "proteus.text@1",
            "seed": "proteus.artifact_ref@1",
            "history": "proteus.evidence.view@1",
        }
        and dict(stage.output_types) == {"response": "proteus.actor_result@1"},
        "proposer ports differ",
    )
    require(
        stage.max_attempts == 1 and stage.failure_policy == "record" and stage.evidence is not None,
        "Meta-Harness records failed proposer attempts and requires explicit history",
    )
    evaluations = {e.id: e for e in recipe.evaluations}
    ids = [key for d in options.datasets for key in (d.train, d.validation, d.test)]
    require(
        len(ids) == len(set(ids)) and set(ids) == set(evaluations),
        "dataset evaluation IDs overlap or are unused",
    )
    visible = set()
    for profile in options.datasets:
        train, validation, test = (
            evaluations[key] for key in (profile.train, profile.validation, profile.test)
        )
        require(
            train.purpose == "optimization"
            and train.visibility == FeedbackVisibility.OBSERVED
            and train.feedback_timing == "after_prediction"
            and train.allow_supervision == (options.mode == "offline"),
            "training disclosure differs from learning mode",
        )
        require(
            validation.purpose == "validation"
            and validation.visibility == FeedbackVisibility.OBSERVED
            and validation.feedback_timing == "after_batch"
            and validation.selection_eligible,
            "Meta-Harness selects using complete observed validation cohorts",
        )
        require(test.purpose == "test", "final evaluation must be hidden test")
        require(
            all(
                e.tasks.order == "declared" and e.tasks.seed == profile.seed
                for e in (train, validation, test)
            ),
            "Meta-Harness preserves prepared dataset order and seed",
        )
        require(
            len({(e.tasks.dataset_hash, e.tasks.split) for e in (train, validation, test)}) == 3,
            "Meta-Harness training, validation and test pools must be disjoint",
        )
        for evaluation in (train, validation, test):
            pool = make_batch(
                registry.create(evaluation.tasks.suite, "task_suite").load(), evaluation.tasks
            )
            require(
                not pool.initial_state.payload,
                "Meta-Harness memory must come from its explicit saved state",
            )
        visible.update((profile.train, profile.validation))
    require(
        set(stage.evidence.evaluation_ids) == visible,
        "proposer history must cover all public training/validation pools",
    )
    require(
        all(actor.session.value == "fresh" for actor in recipe.actors.values()),
        "this profile uses fresh actor sessions",
    )
    model = recipe.actors[recipe.subject.actor]
    require(
        model.prompt_hash is None and not model.tools,
        "Meta-Harness candidate model uses the upstream prompt without extra static prompts or tools",
    )


def artifact_for(services, library, name):
    require("text_classification/agents/" + name + ".py" in library, "candidate module is missing")
    files = {**library, "selected.json": FileContent(canonical_json({"system": name}).encode())}
    return services.artifacts.put(PluginRef(name="proteus.file_tree", version="1"), files)


def parse_proposals(files, iteration, existing):
    """The reference prompt requests three; its controller accepts a variable count."""
    manifest = read_json(files["pending_eval.json"].data.decode())
    require(
        type(manifest) is dict
        and type(manifest.get("iteration")) is int
        and manifest["iteration"] == iteration
        and type(manifest.get("candidates")) is list,
        "invalid pending_eval manifest",
    )
    proposals, names = [], set()
    for row in manifest["candidates"]:
        require(type(row) is dict, "proposal must be an object")
        name = row.get("name")
        require(
            type(name) is str
            and name.isidentifier()
            and not name.startswith("_")
            and name not in existing
            and name not in names,
            "proposal name must be new and unique",
        )
        require(
            row.get("file") == "agents/" + name + ".py" and row["file"] in files,
            "proposal file is missing or mismatched",
        )
        require(
            type(row.get("hypothesis")) is str
            and type(row.get("base_system")) is str
            and row.get("axis") in ("exploitation", "exploration")
            and type(row.get("components")) is list
            and all(type(c) is str for c in row["components"]),
            "proposal metadata differs from the reference contract",
        )
        names.add(name)
        proposals.append(row)
    return proposals


def proposer_seed(services, library, selection, summaries, results, reports, options):
    files = dict(library)
    files["config.yaml"] = FileContent(
        canonical_json(
            {
                "datasets": sorted({d.name for d in options.datasets}),
                "memory_systems": {"baselines": options.baselines},
                "inner_loop": {
                    "mode": options.mode,
                    "num_epochs": options.num_epochs,
                    "batch_size": options.batch_size,
                },
            }
        ).encode()
    )
    files["logs/frontier_val.json"] = FileContent(canonical_json(selection).encode())
    files["logs/evolution_summary.jsonl"] = FileContent(
        ("\n".join(canonical_json(r) for r in summaries) + "\n").encode()
    )
    for result in results:
        prefix = f"logs/{result.dataset}/{result.system}/seed-{result.seed}/"
        files[prefix + "summary.json"] = FileContent(result.to_json().encode())
        # These results are exclusively public optimization/validation records.
        learning = [services.values.get(ref).to_dict() for ref in result.learning]
        files[prefix + "learning.jsonl"] = FileContent(
            ("\n".join(canonical_json(r) for r in learning) + "\n").encode()
        )
    files.update({"logs/reports/" + name: value for name, value in reports.items()})
    return services.artifacts.put(PluginRef(name="proteus.file_tree", version="1"), files)


def proposer_prompt(iteration, options):
    protocol = (Path(__file__).with_name("PROPOSER.md")).read_text()
    return (
        protocol
        + f"""

## Proteus runtime and directory adaptation (iteration {iteration})
These runtime paths override source-checkout paths in the reference protocol above.
There are {len({d.name for d in options.datasets})} datasets.
The controlling coding harness is infrastructure; evolve memory-system source files.
Read /workspace/task/input/config.yaml and /workspace/task/input/logs/ for summaries,
frontier and saved memory/learning records. All disclosed raw code, prediction traces,
and released scores are under /workspace/task/history/; view.json maps their provenance.
Read as many historical systems as useful. No parent is sampled for you.
Starting Python sources are /workspace/task/input/text_classification/.
Input and history are read-only. Copy needed sources into /workspace/task/scratch/
for prototyping and import checks. Python can run these stdlib memory classes;
the original uv project and model transport are replaced by managed execution.
Write each new system to /workspace/task/output/agents/<name>.py, and write the
manifest to /workspace/task/output/pending_eval.json with iteration={iteration}.
Use new unique module names. Shared Python helpers may also go under output/agents/.
Write new post-evaluation reports under output/reports/. These will be retained.
Only output/ is captured. Your final prose is not the candidate submission.
Put the actual implementations and required helpers under output/agents/. Each
declared module must expose a concrete MemorySystem subclass; do not submit a
forwarding import of that same destination module. The controller places these files under
text_classification/agents/ in the candidate package, so relative imports such as
from ..memory_system import MemorySystem are correct. Do not create a mirrored
output/text_classification/ tree: that tree is not merged into the candidate.
Before finishing, check the exact submitted package with the existing loader:
copy input/candidate.py and input/text_classification/ into a fresh scratch
directory, overlay output/agents/ onto its text_classification/agents/, and write
selected.json with system set to each manifest candidate's name in turn. For each
name, run a fresh Python process from that scratch directory and instantiate
candidate.Candidate with a model-free mock callable returning a JSON response.
This checks import/class/constructor compatibility only; it is not a real-model
evaluation. Do not validate a different mirror of the submitted files.
The inherited MemorySystem/LLMCallable interface remains unchanged. Model calls in
the evaluated candidate are managed by Proteus; dependencies must exist in its
configured candidate image. Do not access hidden test data or run final benchmarks.
"""
    )


class MetaHarnessMethod:
    def __init__(self, options):
        self.options = options

    def run(self, services, restored_state):
        # Durable service replay reconstructs this deterministic control flow;
        # settled actor/code/evaluation calls are never inferred or reissued.
        require(restored_state is None, "this profile restores via its operation journal")
        options = self.options
        initial = services.initial_candidate()
        library = dict(services.artifacts.files(initial.artifact))
        require(
            json.loads(library["selected.json"].data)["system"] == options.baselines[0],
            "initial artifact must select the first declared baseline",
        )
        candidates = {options.baselines[0]: initial}
        for name in options.baselines[1:]:
            candidates[name] = services.create_candidate(
                artifact_for(services, library, name), candidate_id=name
            )
        results, result_refs, iterations, summaries, reports = [], [], [], [], {}

        def run_system(name):
            candidate = candidates[name]
            checked = services.invoke_candidate(
                candidate,
                CodeCall(
                    method="inspect", state=CodeState(artifact_hash=candidate.artifact.content_hash)
                ),
            )
            if checked.status == Outcome.INFRA_ERROR:
                raise RuntimeError(checked.diagnostic)
            if checked.status != Outcome.SUCCEEDED:
                return checked.diagnostic or "candidate interface validation failed"
            for profile in options.datasets:
                result = benchmark(services, candidate, name, profile, options)
                results.append(result)
                result_refs.append(services.values.publish("proteus.meta.benchmark@1", result))
            return ""

        for name in options.baselines:
            require(not run_system(name), "baseline failed candidate interface validation")
        selection = frontier(results, options.datasets)
        for iteration in range(1, options.iterations + 1):
            history = services.evidence_view(options.proposer_stage)
            seed = proposer_seed(services, library, selection, summaries, results, reports, options)
            stage = services.stage(
                options.proposer_stage,
                {
                    "prompt": services.values.publish(
                        "proteus.text@1", Text(text=proposer_prompt(iteration, options))
                    ),
                    "seed": services.values.publish("proteus.artifact_ref@1", seed),
                    "history": history,
                },
            )
            response = stage.outputs.get("response")
            diagnostics, proposed = [], []
            if stage.status != OperationStatus.COMPLETED:
                diagnostics.append(stage.diagnostic or "proposer failed")
            else:
                actor = services.values.get(response)
                files = services.artifacts.files(actor.workspace_artifact)
                try:
                    proposed = parse_proposals(
                        files, iteration, {*candidates, *(c.id for c in candidates.values())}
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    diagnostics.append(f"invalid proposal manifest: {exc}")
                if proposed:
                    for path, item in files.items():
                        if path.startswith("agents/"):
                            library["text_classification/" + path] = item
                        elif path.startswith("reports/"):
                            reports[path.removeprefix("reports/")] = item
                    for proposal in proposed:
                        name = proposal["name"]
                        # Upstream has no formal parent-selection policy. Preserve
                        # its declared base_system in metadata, not invented lineage.
                        candidate = services.create_candidate(
                            artifact_for(services, library, name), candidate_id=name
                        )
                        candidates[name] = candidate
                        diagnostic = run_system(name)
                        if diagnostic:
                            diagnostics.append(name + ": " + diagnostic)
                        selected_results = [r for r in results if r.system == name]
                        complete = len(selected_results) == len(options.datasets) and all(
                            r.complete for r in selected_results
                        )
                        scores = []
                        if complete:
                            for dataset in {d.name for d in options.datasets}:
                                group = [r for r in selected_results if r.dataset == dataset]
                                scores.append(
                                    sum(r.correct for r in group)
                                    / sum(r.total for r in group)
                                    * 100
                                )
                        summaries.append(
                            {
                                "iteration": iteration,
                                "system": name,
                                "avg_val": sum(scores) / len(scores) if scores else None,
                                "axis": proposal["axis"],
                                "hypothesis": proposal["hypothesis"],
                                "components": proposal["components"],
                                "base_system": proposal["base_system"],
                                "complete": complete,
                                "diagnostic": diagnostic,
                            }
                        )
            selection = frontier(results, options.datasets)
            record = IterationResult(
                iteration=iteration,
                proposer=response,
                proposals=tuple(proposed),
                diagnostics=tuple(diagnostics),
                frontier=selection,
            )
            iterations.append(services.values.publish("proteus.meta.iteration@1", record))
        selected = final_systems(options.baselines, selection) if options.finalize else ()
        # Commit the frozen selection before any hidden-test dispatch. No code
        # below this boundary calls the proposer or performs further learning.
        frozen = services.values.publish(
            "proteus.meta.summary@1",
            MetaSummary(
                benchmarks=tuple(result_refs),
                iterations=tuple(iterations),
                frontier=selection,
                selected=selected,
                tests={},
                finalized=False,
            ),
        )
        services._record(
            "meta.freeze", {"selection": frozen}, lambda: frozen, decode=type(frozen).from_dict
        )
        tests = {}
        for name in selected:
            for profile in options.datasets:
                result = next(
                    (
                        r
                        for r in results
                        if (r.system, r.dataset, r.seed) == (name, profile.name, profile.seed)
                    ),
                    None,
                )
                require(
                    result is not None and result.complete,
                    "finalization requires every selected validation group",
                )
                ref, _, _, _, valid = evaluate(
                    services, candidates[name], profile.test, result.memory
                )
                require(valid, "final test prediction failed")
                tests[f"{name}/{profile.name}/{profile.seed}"] = ref
        summary = MetaSummary(
            benchmarks=tuple(result_refs),
            iterations=tuple(iterations),
            frontier=selection,
            selected=selected,
            tests=tests,
            finalized=options.finalize,
        )
        return MethodResult(
            outputs={"summary": services.values.publish("proteus.meta.summary@1", summary)}
        )


def register(registry):
    from proteus.methods.meta_harness.grading import MetaGrade, MetaGradeOptions

    root = Path(__file__).parent
    digest = implementation_hash(
        {
            str(p.relative_to(root)): p
            for p in root.rglob("*")
            if p.is_file() and p.suffix in (".py", ".md", ".txt")
        }
    )
    for name, schema in (
        ("benchmark", BenchmarkResult),
        ("iteration", IterationResult),
        ("summary", MetaSummary),
    ):
        registry.register_data("proteus.meta." + name + "@1", schema)
    registry.register(
        Plugin(
            ref=PluginRef(name="proteus.meta_harness", version="1"),
            kind="method",
            options=MetaOptions,
            factory=MetaHarnessMethod,
            source="proteus",
            implementation_hash=digest,
            capabilities=("run.resume", "run.journal"),
            state_schema=EmptyOptions,
            validate=lambda recipe, options: validate(recipe, options, registry),
        )
    )
    registry.register(
        Plugin(
            ref=PluginRef(name="proteus.meta_grade", version="1"),
            kind="evaluator",
            options=MetaGradeOptions,
            factory=MetaGrade,
            source="proteus",
            implementation_hash=digest,
            capabilities=("evaluator.deterministic",),
        )
    )
