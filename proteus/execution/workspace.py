"""Copy only declared inputs into a coding workspace; publish outputs before settlement.

The actor runtime supplies the process/container boundary. This service does not
give it the study root, private evaluations or an implicit persistent filesystem.
"""

from pathlib import Path

from proteus.artifacts.store import ArtifactStore, publish_bytes
from proteus.core.contracts import OperationStatus, require
from proteus.core.serialization import canonical_json
from proteus.evidence.service import EvidenceService
from proteus.execution.workspace_records import WorkspaceInput


class WorkspaceService:
    def __init__(self, services):
        self.services = services
        self.evidence = EvidenceService(services)

    def authorize(self, stage, operation, request):
        require(type(request) is WorkspaceInput, 'workspace request must be typed')
        seeds = [self.services.values.get(ref) for ref in operation.inputs.values()
                 if ref.kind == 'proteus.artifact_ref@1']
        require(request.seed in seeds, 'workspace seed was not supplied to this stage')
        require(request.history in operation.inputs.values(), 'workspace history was not supplied to this stage')
        view = self.services.values.get(request.history)
        self.evidence.bound(view, stage.id, verify_files=True)
        self.services.artifacts.validate(request.seed)

    def prepare(self, request, workspace, *, reserved_recovery=False):
        import shutil
        task = Path(workspace) / 'task'
        # The caller permits this only while its durable actor entry is still
        # reserved: no runtime was dispatched, so partial input copying is safe
        # to redo. Started calls never enter this branch.
        if reserved_recovery and task.exists() and not task.is_symlink():
            shutil.rmtree(task)
        require(not task.exists() and not task.is_symlink(), 'coding actor needs a fresh task workspace')
        task.mkdir()
        self.services.artifacts.materialize(request.seed, task / 'input')
        self.evidence.store.materialize(request.history, task / 'history')
        (task / 'output').mkdir()
        (task / 'scratch').mkdir()
        # Outside the mounted task tree: the actor cannot replace its provenance.
        publish_bytes(Path(workspace) / 'workspace-input.json', (canonical_json(request) + '\n').encode())

    def finish(self, request, workspace, result):
        from dataclasses import replace
        require(result.workspace_artifact is None, 'only the service may publish a coding actor artifact')
        task = Path(workspace) / 'task'
        require(task.is_dir() and not task.is_symlink(), 'coding task workspace was replaced')
        self.services.artifacts.verify_tree(request.seed, task / 'input')
        self.evidence.store.verify_materialized(request.history, task / 'history')
        if result.status != OperationStatus.COMPLETED:
            return result
        files = ArtifactStore.read_tree(task / 'output', max_bytes=request.max_output_bytes,
                                       max_files=request.max_output_files)
        require(bool(files), 'coding actor completed without output files')
        artifact = self.services.artifacts.put(request.seed.codec, files)
        return replace(result, workspace_artifact=artifact)

    def verify_result(self, request, result):
        if result.workspace_artifact is not None:
            require(request is not None and result.status == OperationStatus.COMPLETED,
                    'unexpected coding workspace artifact')
            artifact = result.workspace_artifact
            require(artifact.codec == request.seed.codec, 'coding output codec differs from request')
            require(len(artifact.components) <= request.max_output_files
                    and sum(item.size_bytes for item in artifact.components) <= request.max_output_bytes,
                    'coding output exceeds its declared limits')
            self.services.artifacts.validate(artifact)
        elif request is not None and result.status == OperationStatus.COMPLETED:
            raise ValueError('completed coding actor has no immutable workspace artifact')


class WorkspaceStage:
    def __init__(self, options):
        self.options = options

    def run(self, context, inputs):
        request = WorkspaceInput(seed=inputs['seed'], history=inputs['history'].ref(),
            max_output_bytes=self.options.max_output_bytes, max_output_files=self.options.max_output_files)
        result = context.actor(inputs['prompt'].text, workspace=request)
        require(result.status == OperationStatus.COMPLETED, result.diagnostic or 'coding actor failed')
        return {'response': result}
