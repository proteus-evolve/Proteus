"""Harbor lifecycle with a credential-free, offline container code protocol."""

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
import subprocess
import uuid

from proteus.artifacts.store import _read, publish_bytes
from proteus.core.contracts import Outcome, StatePolicy, require
from proteus.core.recovery import RecoveryRequired
from proteus.core.serialization import canonical_json, content_hash, read_json
from proteus.evaluation.records import Trace
from proteus.execution.builds import image_pin
from proteus.execution.code_rpc import invoke
from proteus.execution.harbor import HarborEnvironment, HarborEnvironmentOptions, _stop_worker, validate_environment
from proteus.execution.harbor_code_worker import cleanup_record


WORKER = Path(__file__).with_name('harbor_code_worker.py')


@dataclass(frozen=True, kw_only=True)
class HarborCodeOptions(HarborEnvironmentOptions):
    max_pids: int = field(default=64, metadata={'minimum': 8})


def validate_harbor_code(execution, options):
    # Explicit memory is carried by CodeState; every sequence step still gets a new container.
    require(execution.state_policy in (StatePolicy.INDEPENDENT, StatePolicy.SEQUENCE),
            'Harbor code uses independent containers with explicit sequence memory')
    validate_environment(replace(execution, state_policy=StatePolicy.INDEPENDENT,
                                 environment=replace(execution.environment, lifecycle='trial')), options)
    env = execution.environment
    image_pin(env.image_digest)
    require(env.network == 'none' and not env.allowed_hosts, 'managed Harbor code requires no-network containers')
    require(set(env.resources) == {'cpus', 'memory_mb'}, 'Harbor code requires explicit CPU and memory limits')
    require(options.bridge_timeout_seconds >= execution.timeout_seconds,
            'Harbor bridge timeout must cover the code invocation deadline')


class HarborCodeEnvironment(HarborEnvironment):
    @contextmanager
    def open(self, artifact, store, root, execution, *, build=None):
        with super().open(artifact, store, root, execution, build=build) as lease:
            def execute(options, call, actor):
                return invoke_harbor_code(lease, artifact, execution, options, call, actor)
            yield replace(lease, code_executor=execute)


def invoke_harbor_code(lease, artifact, execution, options, call, actor):
    handle = lease.handle
    private = handle.root / 'code-private'
    private.mkdir(mode=0o700)
    request_path = private / 'request.json'
    request = {
        'version': 1, 'runtime_hash': handle.options.runtime_hash,
        'candidate_dir': str(lease.local_root.resolve()), 'artifact': artifact.to_dict(),
        'image_digest': execution.environment.image_digest,
        'agent_image': handle.build.image_id if handle.build else execution.environment.image_digest,
        'build_receipt': handle.build.to_dict() if handle.build else None,
        'resources': dict(execution.environment.resources), 'max_pids': handle.options.max_pids,
        'trial_name': 'proteus-' + uuid.uuid4().hex,
        'max_frame_bytes': options.max_frame_bytes,
        'setup_timeout_seconds': handle.options.setup_timeout_seconds,
        'code_worker': str(Path(__file__).with_name('code_worker.py')),
    }

    def launch(fd, deadline):
        import os
        request.update(parent_pid=os.getpid(), deadline=deadline)
        publish_bytes(request_path, (canonical_json(request) + '\n').encode())
        return subprocess.Popen([handle.options.python, '-I', str(WORKER), 'run', str(request_path), str(fd)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(fd,), start_new_session=True)

    def stop(process):
        _stop_worker(process)
        cleanup = cleanup_record(request_path, request)
        if not cleanup['cleanup']['verified']:
            raise RecoveryRequired('Harbor code cleanup is unverified; inspect the owned resource receipt before resuming')

    reply = invoke(lease.local_root, options, call, actor, timeout_seconds=execution.timeout_seconds,
                   launch=launch, stop=stop)
    error_path, proof_path = private / 'worker-error.json', private / 'loaded.json'
    if error_path.exists() and reply.status not in (Outcome.TIMED_OUT, Outcome.INFRA_ERROR):
        error = read_json(_read(error_path).decode())
        require(error['request_hash'] == content_hash(request), 'Harbor code error belongs to another dispatch')
        reply = replace(reply, status=Outcome.INFRA_ERROR, output=None, state=call.state.value,
                        diagnostic=error['diagnostic'])
    if proof_path.exists():
        proof = read_json(_read(proof_path).decode())
        require(proof['request_hash'] == content_hash(request)
                and proof['loaded_artifact_hash'] == artifact.content_hash,
                'Harbor code loaded proof differs from its dispatch')
        reply = replace(reply, trace=Trace(events=(*reply.trace.events, {'kind': 'sandbox', **proof})))
    elif reply.status == Outcome.SUCCEEDED:
        raise ValueError('successful Harbor code execution lacks loaded-container evidence')
    return reply
