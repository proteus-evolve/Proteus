"""Standalone pinned-Harbor research worker and independent resource guard."""

import asyncio
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit


_spec = importlib.util.spec_from_file_location('proteus_harbor_helper',
    Path(__file__).parents[1]/'execution/harbor_worker.py')
_helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helper)
canonical, digest, write_json = _helper.canonical, _helper.digest, _helper.write_json
docker, cleanup_owned_resources = _helper.docker, _helper.cleanup_owned_resources


def emit(value):
    print(canonical(value), flush=True)


def compose(request):
    options = request['options']
    service = {'image': options['image_digest'], 'entrypoint': [], 'command': ['sh', '-c', 'sleep infinity'],
        'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'], 'pids_limit': options['max_pids'],
        'privileged': False, 'init': True,
        'volumes': [{'type': 'bind', 'source': request['workspace'], 'target': '/workspace',
                     'bind': {'create_host_path': False}}]}
    if options['network'] == 'none':
        service['network_mode'] = 'none'
    else:
        service['extra_hosts'] = ['host.docker.internal:host-gateway']
    return {'services': {'main': service}}


def environment_class(network):
    from harbor.environments.docker.docker import DockerEnvironment
    if network == 'public':
        return DockerEnvironment
    from harbor.environments.capabilities import EnvironmentCapabilities
    from harbor.models.task.config import NetworkMode

    class OfflineDockerEnvironment(DockerEnvironment):
        @staticmethod
        def _requires_egress_control(**kwargs):
            return False

        @property
        def capabilities(self):
            return EnvironmentCapabilities(disable_internet=True, docker_compose=True)

        def validate_network_policy_support(self, network_policy=None):
            policy = network_policy or self.network_policy
            if policy.network_mode != NetworkMode.NO_NETWORK or policy.allowed_hosts:
                raise ValueError('offline research worker requires no-network')

        async def _apply_network_policy(self, network_policy):
            self.validate_network_policy_support(network_policy)

    return OfflineDockerEnvironment


def isolation_proof(container_id, request):
    row = json.loads(docker('inspect', container_id))[0]
    host, options = row['HostConfig'], request['options']
    mounts = [{'source': item['Source'], 'destination': item['Destination'], 'type': item['Type'], 'rw': item['RW']}
              for item in row['Mounts']]
    expected = [{'source': request['workspace'], 'destination': '/workspace', 'type': 'bind', 'rw': True}]
    if mounts != expected:
        raise ValueError('worker must mount exactly its own workspace')
    if host['Privileged'] or host.get('CapAdd') or 'ALL' not in host.get('CapDrop', []):
        raise ValueError('worker container capabilities differ')
    if not any(item.startswith('no-new-privileges') for item in host.get('SecurityOpt', [])):
        raise ValueError('worker container can gain privileges')
    if host['PidsLimit'] != options['max_pids'] or host['NetworkMode'] in ('host', 'container'):
        raise ValueError('worker process/network isolation differs')
    if (options['network'] == 'none') != (host['NetworkMode'] == 'none'):
        raise ValueError('worker network declaration differs')
    if options['network'] == 'public' and host['NetworkMode'] != request['trial_name']+'__env_default':
        raise ValueError('worker does not use its own Compose network')
    return {'mounts': mounts, 'network_mode': host['NetworkMode'], 'max_pids': host['PidsLimit'],
            'privileged': False, 'cap_drop': host['CapDrop'], 'security_opt': host['SecurityOpt']}


def guard(path, descriptor, parent_pid):
    request = json.loads(path.read_text())
    root = path.parent
    options = request['options']
    deadline = time.monotonic()+options['timeout_seconds']+options['shutdown_grace_seconds']+35
    clients, pending = set(), b''
    with (root/'harbor-guard.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        while time.monotonic()<deadline:
            if not select.select([descriptor], [], [], .05)[0]:
                continue
            data = os.read(descriptor, 65536)
            if not data:
                break
            pending += data
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                record = json.loads(line)
                if record['event'] == 'client_start':
                    clients.add(record['pid'])
                elif record['event'] == 'client_exit':
                    clients.discard(record['pid'])
        os.close(descriptor)
        # Only live processes spawned in this invocation are signalled. No
        # resumed controller reads an old PID file and signals it.
        finish_by = time.monotonic()+2
        while time.monotonic()<finish_by:
            try:
                os.kill(parent_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.025)
        for pid in (parent_pid, *clients):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        cleanup = cleanup_owned_resources(request['trial_name'])
        write_json(root/'harbor-cleanup.json', {'request_hash': digest(request), 'cleanup': cleanup})


def container_environment(request):
    options = request['options']
    environment = {key: os.environ[key] for key in options['credential_env']}
    if 'PROTEUS_EVALUATION_URL' in os.environ:
        if options['network'] != 'public':
            raise ValueError('offline container cannot access the evaluation gateway')
        address = urlsplit(os.environ['PROTEUS_EVALUATION_URL'])
        if address.scheme != 'http' or address.hostname != '127.0.0.1' or not address.port or address.path:
            raise ValueError('unexpected local gateway address')
        environment['PROTEUS_EVALUATION_URL'] = urlunsplit(('http', f'host.docker.internal:{address.port}', '', '', ''))
        environment['PROTEUS_EVALUATION_TOKEN'] = os.environ['PROTEUS_EVALUATION_TOKEN']
    return environment


async def run(path, request, launch, guard_fd, interrupted):
    from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
    from harbor.models.trial.paths import TrialPaths

    options, root = request['options'], path.parent
    if _helper.runtime_identity()['runtime_hash'] != options['runtime_hash']:
        raise ValueError('Harbor runtime fingerprint changed')
    if digest(launch['identity']) != digest(json.loads((root/'request.json').read_text())['identity']):
        raise ValueError('container launch identity differs')
    environment_dir = root/'harbor-environment'
    environment_dir.mkdir()
    write_json(environment_dir/'docker-compose.yaml', compose(request))
    network = NetworkMode.NO_NETWORK if options['network'] == 'none' else NetworkMode.PUBLIC
    environment = environment_class(options['network'])(environment_dir=environment_dir,
        environment_name='proteus-research-worker', session_id=request['trial_name']+'__env',
        trial_paths=TrialPaths(root/'harbor'),
        task_env_config=EnvironmentConfig(docker_image=options['image_digest'],
                                        cpus=options['cpus'], memory_mb=options['memory_mb']),
        network_policy=NetworkPolicy(network_mode=network), mounts=[], enable_environment_dir_upload=False)
    process, output_thread, container_id, child_pid = None, None, None, None
    try:
        write_json(root/'harbor-dispatched.json', {'request_hash': digest(request)})

        async def setup():
            nonlocal container_id
            await environment.start(force_build=False)
            proof_request = {'image_digest': options['image_digest'],
                             'resources': {'cpus': options['cpus'], 'memory_mb': options['memory_mb']}}
            actual = await _helper.checked_environment(proof_request, environment, {'/workspace'})
            container_id = actual['container_id']
            proof = await asyncio.to_thread(isolation_proof, container_id, request)
            for source, target in ((request['bootstrap'], '/opt/proteus/container_worker.py'),
                                   (request['codex_bridge'], '/opt/proteus/codex_bridge.py')):
                await environment.upload_file(source_path=Path(source), target_path=target)
            result = await environment.exec('chmod -R a+rX /opt/proteus', timeout_sec=15)
            if result.return_code:
                raise ValueError('container bootstrap is not readable')
            write_json(root/'harbor-loaded.json', {'request_hash': digest(request), **actual, **proof})

        await asyncio.wait_for(setup(), options['setup_timeout_seconds'])
        if interrupted():
            raise InterruptedError('worker interrupted during environment setup')
        process = subprocess.Popen(['docker', 'exec', '-i', '--user', options['user'], '--workdir', '/workspace',
            container_id, 'env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin', 'python3', '-I',
            '/opt/proteus/container_worker.py'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=sys.stderr, start_new_session=True)
        os.write(guard_fd, (canonical({'event': 'client_start', 'pid': process.pid})+'\n').encode())
        payload = canonical({'launch': launch, 'command': options['command'],
                             'command_files': options['command_files'], 'environment': container_environment(request)})

        def send():
            process.stdin.write((payload+'\n').encode())
            process.stdin.close()
        await asyncio.wait_for(asyncio.to_thread(send), options['setup_timeout_seconds'])
        ready = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), options['setup_timeout_seconds'])
        header = json.loads(ready)
        if set(header) != {'worker_pid'} or type(header['worker_pid']) is not int or header['worker_pid'] < 2:
            raise ValueError('container worker did not confirm its process group')
        child_pid = header['worker_pid']

        def forward():
            while chunk := os.read(process.stdout.fileno(), 65536):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        output_thread = threading.Thread(target=forward, daemon=True)
        output_thread.start()
        stopped = None
        while True:
            if process.poll() is not None:
                break
            if interrupted() and stopped is None:
                code = 'import os,signal; os.killpg('+str(child_pid)+',signal.SIGINT)'
                await asyncio.to_thread(docker, 'exec', '--user', options['user'], container_id,
                                        'python3', '-c', code, timeout=10)
                stopped = time.monotonic()
            if stopped is not None and time.monotonic()-stopped >= options['shutdown_grace_seconds']:
                break  # stop the entire owned container in finally
            await asyncio.sleep(.025)
        return 0 if interrupted() else process.returncode
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait)
            os.write(guard_fd, (canonical({'event': 'client_exit', 'pid': process.pid})+'\n').encode())
            if output_thread is not None:
                await asyncio.to_thread(output_thread.join, 2)
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
        # The independent guard seals resource cleanup only after this worker's
        # process group has ended, including any cancelled Compose subprocess.
        try:
            await asyncio.wait_for(environment.stop(delete=True), 30)
        except Exception:
            pass  # guard performs and records the final owned-resource audit


def main():
    mode, path = sys.argv[1], Path(sys.argv[2])
    if mode == 'guard':
        guard(path, int(sys.argv[3]), int(sys.argv[4]))
        return 0
    request = json.loads(path.read_text())
    launch = json.loads(sys.stdin.readline())
    read_fd, write_fd = os.pipe()
    with (path.parent/'harbor-guard.log').open('ab') as guard_log:
        subprocess.Popen([sys.executable, '-I', __file__, 'guard', str(path), str(read_fd), str(os.getpid())],
            pass_fds=(read_fd,), start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=guard_log, stderr=guard_log)
    os.close(read_fd)
    stopped = False

    def interrupt(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGINT, interrupt)
    try:
        return asyncio.run(run(path, request, launch, write_fd, lambda: stopped))
    except BaseException as exc:
        emit({'event': 'protocol_error'})
        print('Harbor worker failed: '+type(exc).__name__+': '+str(exc), file=sys.stderr)
        return 1
    finally:
        os.close(write_fd)


if __name__ == '__main__':
    raise SystemExit(main())
