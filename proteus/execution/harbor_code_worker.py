"""Standalone pinned-Harbor worker and independent owned-container cleanup guard.

The container has no host mounts, network, Docker socket or model credentials.
Only the byte stream of the code protocol crosses `docker exec -i`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

if __package__:
    from proteus.execution.filesystem_policy import MAX_RECEIPT, validate_receipt
    from proteus.execution.harbor_worker import (
        VERIFY_TREE, canonical, checked_environment, cleanup_owned_resources, digest, docker, runtime_identity, write_json,
    )
else:
    # Adding this directory to sys.path would shadow the real Harbor package
    # with Proteus's sibling harbor.py in the independent runtime.
    import importlib.util
    _spec = importlib.util.spec_from_file_location('proteus_harbor_helper', Path(__file__).with_name('harbor_worker.py'))
    _helper = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_helper)
    VERIFY_TREE = _helper.VERIFY_TREE
    _policy_spec = importlib.util.spec_from_file_location('proteus_filesystem_policy', Path(__file__).with_name('filesystem_policy.py'))
    _policy = importlib.util.module_from_spec(_policy_spec)
    _policy_spec.loader.exec_module(_policy)
    MAX_RECEIPT, validate_receipt = _policy.MAX_RECEIPT, _policy.validate_receipt
    canonical, checked_environment = _helper.canonical, _helper.checked_environment
    cleanup_owned_resources, digest = _helper.cleanup_owned_resources, _helper.digest
    docker, runtime_identity, write_json = _helper.docker, _helper.runtime_identity, _helper.write_json


def cleanup_record(path, request):
    """Serialize parent/worker/guard cleanup; only generated Compose labels are touched."""
    import fcntl
    root = Path(path).parent
    with (root / 'cleanup.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = root / 'cleanup.json'
        if target.exists():
            prior = json.loads(target.read_text())
            if prior['request_hash'] != digest(request):
                raise ValueError('cleanup request identity changed')
            if prior['cleanup']['verified']:
                return prior
        result = {'request_hash': digest(request), 'cleanup': cleanup_owned_resources(request['trial_name'])}
        write_json(target, result)
        return result


def guard(path, fd, worker_pid):
    request = json.loads(path.read_text())
    while time.monotonic() < request['deadline'] + 5:
        if select.select([fd], [], [], .1)[0] and not os.read(fd, 1):
            break
        try:
            os.kill(request['parent_pid'], 0)
        except ProcessLookupError:
            break
    os.close(fd)
    def sealed():
        completed = path.parent / 'cleanup.json'
        if not completed.exists():
            return False
        receipt = json.loads(completed.read_text())
        return receipt.get('request_hash') == digest(request) and receipt.get('cleanup', {}).get('verified', False)

    # The worker closes its pipe immediately before interpreter exit. Let a
    # normal exit finish; the controller can then reap it and audit cleanup.
    # Only controller/guard receipts count: the worker must never seal one while
    # a cancelled Harbor subprocess could still mutate the Docker project.
    finishing = path.parent / 'worker-finished.json'
    if finishing.exists() and json.loads(finishing.read_text()).get('request_hash') == digest(request):
        until = min(time.monotonic() + 2, request['deadline'] + 5)
        while time.monotonic() < until:
            if sealed():
                return
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
    # Reap the complete worker process group BEFORE resource cleanup, so an
    # in-flight Compose command cannot create another container after the audit.
    if sealed():
        return
    try:
        os.killpg(worker_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    cleanup_record(path, request)


def offline_environment_class():
    from harbor.environments.capabilities import EnvironmentCapabilities
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import NetworkMode

    class OfflineDockerEnvironment(DockerEnvironment):
        # A fixed Docker `none` namespace needs no proxy or egress sidecar. The
        # sealed Compose definition and inspected HostConfig enforce this policy.
        @staticmethod
        def _requires_egress_control(**kwargs):
            return False

        @property
        def capabilities(self):
            return EnvironmentCapabilities(disable_internet=True, docker_compose=True)

        def validate_network_policy_support(self, network_policy=None):
            policy = network_policy or self.network_policy
            if policy.network_mode != NetworkMode.NO_NETWORK or policy.allowed_hosts:
                raise ValueError('this fixed Harbor code provider supports no-network only')

        async def _apply_network_policy(self, network_policy):
            self.validate_network_policy_support(network_policy)

    return OfflineDockerEnvironment


def compose(request):
    return {'services': {'main': {
        'image': request['agent_image'], 'entrypoint': [], 'command': ['sh', '-c', 'sleep infinity'],
        'network_mode': 'none', 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'pids_limit': request['max_pids'], 'privileged': False, 'init': True,
    }}}


def isolation_proof(container_id, request):
    row = json.loads(docker('inspect', container_id))[0]
    config = row['HostConfig']
    proof = {'network_mode': config['NetworkMode'], 'mount_destinations': [m['Destination'] for m in row['Mounts']],
        'privileged': config['Privileged'], 'cap_drop': config['CapDrop'], 'security_opt': config['SecurityOpt'],
        'pids_limit': config['PidsLimit'], 'pid_mode': config['PidMode'], 'candidate_user': '65534:65534'}
    if (proof['network_mode'] != 'none' or proof['mount_destinations'] or proof['privileged']
            or set(proof['cap_drop'] or []) != {'ALL'} or proof['pids_limit'] != request['max_pids']
            or proof['pid_mode'] or 'no-new-privileges:true' not in (proof['security_opt'] or [])):
        raise ValueError('Docker did not apply the code isolation policy')
    return proof


def relay(process, channel):
    def incoming():
        try:
            while data := channel.recv(65536):
                view = memoryview(data)
                while view:
                    written = process.stdin.write(view)
                    if not written:
                        raise OSError('closed candidate protocol stream')
                    view = view[written:]
                process.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    def outgoing(source, target):
        try:
            while data := source.read(65536):
                target(data)
        except (OSError, ValueError):
            pass

    def diagnostic(data):
        sys.stderr.buffer.write(data)
        sys.stderr.buffer.flush()

    threads = [threading.Thread(target=incoming, daemon=True),
        threading.Thread(target=outgoing, args=(process.stdout, channel.sendall), daemon=True),
        threading.Thread(target=outgoing, args=(process.stderr, diagnostic), daemon=True)]
    for thread in threads:
        thread.start()
    return threads


async def run(path, channel, request):
    from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
    from harbor.models.trial.paths import TrialPaths

    if runtime_identity()['runtime_hash'] != request['runtime_hash']:
        raise ValueError('Harbor runtime fingerprint changed')
    root = path.parent
    environment_dir = root / 'environment'
    environment_dir.mkdir()
    write_json(environment_dir / 'docker-compose.yaml', compose(request))
    environment = offline_environment_class()(environment_dir=environment_dir,
        environment_name='proteus-code', session_id=request['trial_name'] + '__env',
        trial_paths=TrialPaths(root / 'harbor'),
        task_env_config=EnvironmentConfig(docker_image=request['agent_image'], **request['resources']),
        network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
        mounts=[], enable_environment_dir_upload=False)
    process, threads = None, []
    try:
        async def setup():
            await environment.start(force_build=False)
            actual = await checked_environment(request, environment, set(), image=request['agent_image'])
            isolation = await asyncio.to_thread(isolation_proof, actual['container_id'], request)
            built = request['build_receipt'] is not None
            if not built:
                await environment.upload_dir(source_dir=Path(request['candidate_dir']), target_dir='/opt/proteus/candidate')
            source_root = '/opt/proteus/source' if built else '/opt/proteus/candidate'
            encoded = base64.b64encode(canonical(request['artifact']).encode()).decode()
            witness = await environment.exec(shlex.join(['python3', '-I', '-c', VERIFY_TREE, encoded, source_root]), timeout_sec=30)
            if witness.return_code:
                raise ValueError('container candidate verification failed')
            await environment.upload_file(source_path=Path(request['code_worker']), target_path='/opt/proteus/code_worker.py')
            worker_hash = 'sha256:' + hashlib.sha256(Path(request['code_worker']).read_bytes()).hexdigest()
            if request.get('filesystem_policy') is not None:
                helper = Path(request['filesystem_helper'])
                if 'sha256:' + hashlib.sha256(helper.read_bytes()).hexdigest() != request['filesystem_helper_hash']:
                    raise ValueError('filesystem policy helper changed before dispatch')
                await environment.upload_file(source_path=helper, target_path='/opt/proteus/filesystem_policy.py')
            execution_root = '/opt/proteus/build' if built else '/opt/proteus/candidate'
            # CAP_CHOWN is deliberately absent. The root-owned upload can be
            # made writable without granting a capability to candidate processes.
            permissions = await environment.exec('chmod -R a+rwX ' + execution_root, timeout_sec=30)
            if permissions.return_code:
                raise ValueError('candidate workspace permissions could not be set')
            proof = {**json.loads(witness.stdout), **actual, **isolation,
                'request_hash': digest(request), 'code_worker_hash': worker_hash,
                'build_receipt_hash': digest(request['build_receipt']) if built else None}
            if request.get('filesystem_policy') is None:
                write_json(root / 'loaded.json', proof)
            return proof, execution_root

        proof, execution_root = await asyncio.wait_for(setup(), min(request['setup_timeout_seconds'],
            max(.001, request['deadline'] - time.monotonic())))
        # Lifecycle, source upload and witness use Harbor. Its buffered exec API
        # has no bidirectional stream, so only this transport uses docker exec.
        remaining = max(.001, request['deadline'] - time.monotonic())
        command = ['python3', '-I', '/opt/proteus/code_worker.py', '--stdio', str(request['max_frame_bytes']), str(remaining)]
        access = request.get('filesystem_policy')
        if access is not None:
            command = ['python3', '-I', '/opt/proteus/filesystem_policy.py',
                       base64.b64encode(canonical(access).encode()).decode(), '--', *command]
        process = subprocess.Popen(['docker', 'exec', '-i', '--user', '65534:65534', '--workdir', execution_root,
            proof['container_id'], 'env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin', *command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        if access is not None:
            line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline, MAX_RECEIPT + 1), remaining)
            if not line.endswith(b'\n') or len(line) > MAX_RECEIPT:
                await asyncio.wait_for(asyncio.to_thread(process.wait),
                                       max(.001, request['deadline'] - time.monotonic()))
                detail = process.stderr.read(4096).decode('utf-8', errors='replace')
                raise ValueError('filesystem policy bootstrap did not confirm enforcement: ' + detail)
            receipt = validate_receipt(json.loads(line), access, uid=65534, gid=65534)
            proof.update(filesystem_policy=receipt, filesystem_helper_hash=request['filesystem_helper_hash'])
            write_json(root / 'loaded.json', proof)
            remaining = max(.001, request['deadline'] - time.monotonic())
        threads = relay(process, channel)
        code = await asyncio.wait_for(asyncio.to_thread(process.wait), remaining)
        for thread in threads[1:]:
            await asyncio.to_thread(thread.join, 2)
        return code
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait)
            # Wake a forwarding thread blocked on the host's next response.
            channel.shutdown(socket.SHUT_RD)
            for thread in threads:
                await asyncio.to_thread(thread.join, 2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        await asyncio.wait_for(environment.stop(delete=True), 30)


def main():
    mode, path = sys.argv[1], Path(sys.argv[2])
    if mode == 'guard':
        guard(path, int(sys.argv[3]), int(sys.argv[4]))
        return 0
    request = json.loads(path.read_text())
    read_fd, write_fd = os.pipe()
    watcher = subprocess.Popen([sys.executable, '-I', __file__, 'guard', str(path), str(read_fd), str(os.getpid())],
        pass_fds=(read_fd,), start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(read_fd)
    write_json(path.parent / 'watchdog.json', {'pid': watcher.pid, 'worker_pid': os.getpid(), 'request_hash': digest(request)})
    channel = socket.socket(fileno=int(sys.argv[3]))
    try:
        return asyncio.run(run(path, channel, request))
    except BaseException as exc:
        write_json(path.parent / 'worker-error.json', {'request_hash': digest(request),
            'diagnostic': f'{type(exc).__name__}: {exc}'})
        return 1
    finally:
        channel.close()
        write_json(path.parent / 'worker-finished.json', {'request_hash': digest(request)})
        os.close(write_fd)
        # The guard must outlive this group. It or the controller kills remaining
        # group members before sealing the final resource audit; never wait for
        # it here, because a cancellation can leave a Harbor CLI in this group.


if __name__ == '__main__':
    sys.exit(main())
