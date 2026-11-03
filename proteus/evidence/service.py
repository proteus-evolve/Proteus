"""Export only public, released evidence from the evaluation service's committed state."""

from proteus.artifacts.codecs import bytes_hash
from proteus.core.contracts import DataRef, FeedbackVisibility, require
from proteus.core.serialization import canonical_json, content_hash
from proteus.evidence.records import EvidenceEntry, EvidenceView
from proteus.evidence.store import EvidenceStore


def _key(value):
    return content_hash(value).removeprefix("sha256:")


class EvidenceService:
    def __init__(self, services):
        self.services = services
        self.store = EvidenceStore(services.root / "evidence")

    def _stage(self, stage_id):
        stage = next((stage for stage in self.services.recipe.stages if stage.id == stage_id), None)
        require(stage is not None and stage.evidence is not None, "stage has no declared evidence access")
        return stage

    def view(self, stage_id, *, candidate_ids=None):
        stage = self._stage(stage_id)
        if candidate_ids is not None:
            require(type(candidate_ids) is tuple and len(set(candidate_ids)) == len(candidate_ids),
                    "candidate filter must be a unique tuple")
            require(set(candidate_ids) <= {candidate.id for candidate in self.services.candidates},
                    "unknown candidate in evidence filter")
        ref = self.services._record("evidence.view", {"stage_id": stage_id, "candidate_ids": candidate_ids},
            lambda: self._capture(stage, candidate_ids), decode=DataRef.from_dict)
        # Committed replay never recaptures later history; missing/corrupt evidence fails closed.
        self.bound(self.services.values.get(ref), stage_id, verify_files=True)
        return ref

    def bound(self, view: EvidenceView, stage_id: str, *, verify_files=False) -> DataRef:
        stage = self._stage(stage_id)
        services = self.services
        require(type(view) is EvidenceView and view.run_id == services.run_id
                and view.config_hash == services.recipe.config_hash and view.lock_hash == services.lock_hash
                and view.stage_id == stage.id and view.role == stage.role and view.access == stage.evidence,
                "evidence view belongs to another run, stage, role or policy")
        ref = view.ref()
        stored = self.store.verify(ref) if verify_files else self.store.get(ref)
        require(services.values.get(ref) == view and stored == view, "evidence view differs from store")
        return ref

    def _capture(self, stage, candidate_ids):
        services, access = self.services, stage.evidence
        candidates = [item for item in services.candidates if candidate_ids is None or item.id in candidate_ids]
        population = {item.id: item for item in candidates}
        files, entries = {}, []

        def add(path, kind, candidate_id, source, *, data=None, **provenance):
            if kind not in access.kinds:
                return
            data = canonical_json(source).encode() if data is None else data
            require(path not in files, "duplicate evidence file")
            files[path] = data
            entries.append(EvidenceEntry(path=path, kind=kind, candidate_id=candidate_id,
                content_hash=bytes_hash(data), size_bytes=len(data), source_hash=content_hash(source), **provenance))

        for candidate in candidates:
            require(services.artifacts.candidate(services.run_id, candidate.id) == candidate,
                    "evidence candidate differs from artifact store")
            prefix = f"candidates/{_key(candidate.id)}"
            add(f"{prefix}/candidate.json", "candidate", candidate.id, candidate)
            if "artifact" in access.kinds:
                for name, item in services.artifacts.files(candidate.artifact).items():
                    add(f"{prefix}/artifact/{name}", "artifact", candidate.id, candidate.artifact, data=item.data)

        for session in services._evaluation_service.sessions:
            spec = session.spec
            if spec.id not in access.evaluation_ids or spec.tasks.split not in access.splits:
                continue
            require(spec.visibility == FeedbackVisibility.OBSERVED, "private evaluations cannot become stage evidence")
            batches = [batch for batch in services.evaluations if batch.operation_id == session.operation_id]
            rows = {row.rollout_id: row for batch in batches for row in batch.rows}
            for index, observation in enumerate(session._observations):
                rollout = observation.rollout
                if rollout.candidate_id not in population:
                    continue
                require(rollout in services.rollouts and rollout.artifact_hash == population[rollout.candidate_id].artifact.content_hash,
                        "evidence rollout differs from recorded candidate")
                provenance = dict(evaluation_id=spec.id, operation_id=session.operation_id,
                                  split=rollout.split, rollout_id=rollout.id)
                prefix = f"evaluations/{_key(session.operation_id)}/rollouts/{_key(rollout.id)}"
                def record(name, kind, source):
                    add(f"{prefix}/{name}.json", kind, rollout.candidate_id, source, **provenance)
                record("task", "task", session.batch.tasks[index])
                record("prediction", "rollout", rollout)
                for i, ref in enumerate(rollout.trace):
                    if "trace" in access.kinds:
                        record(f"trace-{i}", "trace", services.values.get(ref))
                # after_prediction: only the returned observation's row is released.
                # after_batch: only published batches are released, including after resume.
                row = observation.row if spec.feedback_timing == "after_prediction" else rows.get(rollout.id)
                if row is not None and "feedback" in access.kinds:
                    record("grade", "feedback", row)
                    for i, ref in enumerate(row.feedback):
                        record(f"feedback-{i}", "feedback", services.values.get(ref))
            for batch in batches:
                if batch.candidate_id in population:
                    add(f"evaluations/{_key(session.operation_id)}/batches/{_key(batch.id)}.json",
                        "evaluation", batch.candidate_id, batch, evaluation_id=spec.id,
                        operation_id=session.operation_id, split=spec.tasks.split)
        view = EvidenceView(run_id=services.run_id, config_hash=services.recipe.config_hash, lock_hash=services.lock_hash,
            stage_id=stage.id, role=stage.role, access=access, candidate_ids=tuple(item.id for item in candidates),
            entries=tuple(sorted(entries, key=lambda entry: entry.path)))
        ref = self.store.put(view, files)
        require(services.values.publish("proteus.evidence.view@1", view) == ref, "evidence publication differs")
        return ref
