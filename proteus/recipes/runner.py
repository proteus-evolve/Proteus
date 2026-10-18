"""Execute registered methods under an immutable manifest and a single-writer lease."""

from __future__ import annotations

import json
from pathlib import Path

from proteus.core.contracts import OperationStatus, require
from proteus.core.methods import MethodResult, RunServices, StageFailed
from proteus.core.run_lock import run_lock
from proteus.core.recovery import Journal, RecoveryRequired
from proteus.core.serialization import content_hash
from proteus.recipes.manifest import RunManifest


def _write(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def run_recipe(recipe, *, registry, lock, root, run_id=None, prompts=None, resume=False,
               artifact_source=None):
    registry = registry.snapshot()
    recipe = registry.resolve(recipe)
    registry.check_lock(recipe, lock)
    definition = registry.get(recipe.method.plugin, "method")
    require(not resume or "run.resume" in definition.capabilities,
            "this method does not support recovery yet")

    def validate_state(state):
        if state is None:
            return
        require(state.run_id == manifest.run_id and state.method == recipe.method.plugin
                and state.config_hash == recipe.config_hash,
                "method state belongs to another experiment")
        require(definition.state_schema is not None, "method has no registered state schema")
        require(state.state.namespace == recipe.method.plugin.name
                and state.state.payload_version == definition.state_version,
                "unsupported plugin state version/namespace; migrate explicitly before restoring")
        definition.state_schema.from_dict(state.state.payload)
    root = Path(root).resolve()
    with run_lock(root):
        manifest_path = root / "manifest.json"
        if resume:
            require(manifest_path.is_file(), "resume requires an existing manifest")
            manifest = RunManifest.from_json(manifest_path.read_text(encoding="utf-8"))
            manifest.assert_resume_compatible(recipe, lock)
            require(run_id is None or manifest.run_id == run_id, "resume changes run_id")
        else:
            require(not root.exists() or not any(root.iterdir()), "run directory already contains data")
            manifest = RunManifest.create(run_id=run_id or root.name, recipe=recipe, lock=lock)
        services = RunServices(run_id=manifest.run_id, recipe=recipe, registry=registry,
                               root=root, prompts=prompts, resume=resume, lock_hash=manifest.lock_hash)
        services.native_prompts = []
        # Constructor option validation and role prompt hashes precede publication.
        method = registry.create(recipe.method, "method")
        manifest.write(manifest_path)
        restored = None
        finished = None
        previous = root / "result.json"
        if "run.journal" in definition.capabilities:
            require(definition.state_schema is not None, "journal methods require a state schema")
            services._journal = Journal(services, manifest_hash=manifest.manifest_hash, validate_state=validate_state)
            restored, finished = services._journal.initialize()
        elif resume and previous.is_file():
            restored = MethodResult.from_json(previous.read_text(encoding="utf-8")).state
            validate_state(restored)
        result = None
        uncertain = False
        try:
            if artifact_source is not None:
                from proteus.artifacts.store import ArtifactStore
                require(recipe.subject is not None, "artifact import requires a subject")
                services.artifacts.copy_from(ArtifactStore(artifact_source, registry),
                                             recipe.subject.initial_artifact)
            result = MethodResult.from_dict(finished) if finished is not None else method.run(services, restored)
            require(type(result) is MethodResult, "method must return MethodResult")
            require(all(session.closed for session in services._evaluation_service.sessions),
                    "method left an incomplete evaluation cohort")
            for ref in result.outputs.values():
                services.values.get(ref)
            validate_state(result.state)
        except RecoveryRequired as exc:
            uncertain = True
            result = MethodResult(status=OperationStatus.FAILED, diagnostic=f"RecoveryRequired: {exc}")
        except StageFailed as exc:
            result = MethodResult(status=OperationStatus.FAILED, diagnostic=str(exc))
        except (Exception, SystemExit) as exc:
            result = MethodResult(status=OperationStatus.FAILED,
                                  diagnostic=f"{type(exc).__name__}: {exc}")
        finally:
            uncertain = uncertain or (services._journal is not None and services._journal.pending)
            if not uncertain and (services._journal is None or result is not None):
                services._evaluation_service.finalize_incomplete()
            if services._journal is not None and result is not None and not uncertain and finished is None:
                result = services._journal.finish(result)
            # Keep separate attempt files on native resume; never erase prior evidence.
            attempt = 1
            while (root / f"execution-{attempt:04d}.json").exists():
                attempt += 1
            _write(root / f"execution-{attempt:04d}.json", {
                "manifest_hash": manifest.manifest_hash,
                "requests": [item.to_dict() for item in services.requests],
                "results": [item.to_dict() for item in services.results],
                "actor_calls": services.actor_calls, "native_prompts": services.native_prompts,
                "candidates": [item.to_dict() for item in services.candidates],
                "rollouts": [item.to_dict() for item in services.rollouts],
                "evaluations": [item.to_dict() for item in services.evaluations],
                "reserved_evaluations": services._evaluation_service.reserved,
                # Native opaque episodes still do not claim coverage by this service ledger.
                "ledger": {"path": "private/ledger.sqlite3",
                           "coverage": ["managed_actor_calls", "evaluation_cohort_reservations",
                                        "subject_calls", "grader_calls", "backend_verifiers", "source_builds"]}
                          if services._ledger is not None else None,
                "values": services.values.export(), "values_hash": content_hash(services.values.export()),
            })
            if result is not None:
                _write(previous, result.to_dict())
        return result
