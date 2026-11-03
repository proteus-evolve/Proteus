"""Standalone stdlib bridge for pinned `codex exec --json` and explicit resume IDs.

The supervisor owns timeout, process-group signals, durable completion and usage
settlement. This bridge never launches a second provider process on failure.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import uuid


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def session_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("provider session must be an exact UUID")
    return value


class CodexStream:
    def __init__(self, requested, previous_usage=None):
        self.requested = session_id(requested) if requested is not None else None
        self.session = None
        self.started = False
        self.terminal = None
        self.usage = {"model_calls": None, "input_tokens": None, "output_tokens": None, "cost_usd": None}
        self.totals = {"input_tokens": None, "output_tokens": None}
        self.baseline = {name: 0 if requested is None else (previous_usage or {}).get(name)
                         for name in self.totals}

    def accept(self, event):
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("invalid Codex event")
        kind = event["type"]
        if kind == "thread.started":
            value = session_id(event.get("thread_id"))
            if self.session is not None or (self.requested is not None and self.requested != value):
                raise ValueError("Codex started a different or duplicate thread")
            self.session = value
            return {"event": "session", "session_id": value, "resumed_from": self.requested}
        if kind == "turn.started":
            if self.session is None or self.started:
                raise ValueError("unexpected Codex turn start")
            self.started = True
        if kind in ("turn.completed", "turn.failed"):
            if not self.started or self.terminal is not None:
                raise ValueError("unexpected Codex terminal event")
            self.terminal = kind
            if kind == "turn.completed":
                usage = event.get("usage", {})
                if not isinstance(usage, dict):
                    raise ValueError("invalid Codex usage")
                for name in ("input_tokens", "output_tokens"):
                    value = usage.get(name)
                    if value is not None and (type(value) is not int or value < 0):
                        raise ValueError("invalid Codex token count")
                    baseline = self.baseline[name]
                    if baseline is not None and value is not None:
                        if value < baseline:
                            raise ValueError("Codex cumulative token count decreased")
                        self.usage[name] = value - baseline
                    self.totals[name] = value
        return None

    def result(self, *, interrupted=False):
        if self.session is None:
            raise ValueError("Codex did not confirm a session")
        usage = dict(self.usage)
        if interrupted or self.terminal != "turn.completed":
            usage = {name: None for name in usage}
        totals = dict(self.totals) if not interrupted and self.terminal == "turn.completed" else None
        return {"event": "result", "session_id": self.session, "resumed_from": self.requested,
                "usage": usage, "session_usage": totals}


def command(spec, launch):
    if set(spec) != {"command", "command_files", "model", "network_access", "api_base_url", "api_key_env"}:
        raise ValueError("invalid Codex bridge specification")
    if type(spec["network_access"]) is not bool or not isinstance(spec["model"], str) or not spec["model"].strip():
        raise ValueError("invalid Codex model or network setting")
    args = list(spec["command"])
    if not args or not Path(args[0]).is_absolute() or args[0] not in spec["command_files"]:
        raise ValueError("Codex executable requires a pin")
    for name, pin in spec["command_files"].items():
        if "sha256:" + hashlib.sha256(Path(name).read_bytes()).hexdigest() != pin:
            raise ValueError("Codex command file changed")
    # A shared desktop daemon can retain another provider/configuration. Each
    # worker must run its pinned CLI with its own persistent state and options.
    args += ["--no-daemon", "exec"]
    previous = launch["session_id"]
    if launch["continuity"] == "native":
        args += ["resume", session_id(previous)]
    elif launch["continuity"] not in ("fresh", "fresh_fallback") or previous is not None:
        raise ValueError("invalid Codex continuation request")
    args += ["--json", "--skip-git-repo-check", "--ignore-user-config", "--model", spec["model"]]
    configs = {'approval_policy': '"never"', 'sandbox_mode': '"workspace-write"',
        'sandbox_workspace_write.network_access': str(spec["network_access"]).lower(),
        'shell_environment_policy.inherit': '"all"',
        'shell_environment_policy.ignore_default_excludes': 'true',
        'shell_environment_policy.experimental_use_profile': 'false',
        'agents.enabled': 'false'}
    if spec['api_base_url'] is not None:
        configs.update({'model_provider': '"proteus"',
                        'model_providers.proteus.name': '"Proteus Responses provider"',
                        'model_providers.proteus.base_url': json.dumps(spec['api_base_url']),
                        'model_providers.proteus.wire_api': '"responses"',
                        'model_providers.proteus.env_key': json.dumps(spec['api_key_env'])})
    # Only non-provider shell inputs and this worker's scoped gateway credential
    # are inherited by model-generated commands. Credential values are never argv.
    for name in ("PATH", "LANG", "LC_ALL", "TMPDIR", "PROTEUS_EVALUATION_URL", "PROTEUS_EVALUATION_TOKEN"):
        configs['shell_environment_policy.filters.' + name] = '"include"'
    for key, value in configs.items():
        args += ["-c", key + "=" + value]
    return args + ["-"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    launch = json.loads(sys.stdin.readline())
    spec = json.loads(args.spec)
    interrupted = False

    def interrupt(*_):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, interrupt)
    child = None
    try:
        argv = command(spec, launch)
        stream = CodexStream(launch["session_id"], launch["session_usage"])
        # Dedicated persistent CLI state for this workspace; no personal auth or
        # config is copied. API credentials must be declared in credential_env.
        state = Path.cwd() / ".proteus-codex"
        if state.is_symlink():
            raise ValueError("Codex state directory cannot be a symlink")
        state.mkdir(mode=0o700, exist_ok=True)
        environment = dict(os.environ, CODEX_HOME=str(state))
        with tempfile.TemporaryFile() as prompt:
            prompt.write(launch["prompt"].encode())
            prompt.seek(0)
            if interrupted:
                raise ValueError("worker interrupted before provider dispatch")
            child = subprocess.Popen(argv, stdin=prompt, stdout=subprocess.PIPE, stderr=sys.stderr,
                env=environment)  # stays in the supervisor-owned process group
            while True:
                line = child.stdout.readline(4 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 4 * 1024 * 1024:
                    raise ValueError("truncated or oversized Codex JSON event")
                if not line.endswith(b"\n"):
                    if interrupted:
                        # SIGINT can stop the provider halfway through writing
                        # progress. Keep the earlier confirmed thread, never
                        # interpret the fragment as a terminal usage receipt.
                        emit({"provider": "codex", "interrupted_fragment": line.decode(errors="replace")})
                        break
                    raise ValueError("truncated Codex JSON event")
                event = json.loads(line)
                # Wrapping prevents provider or model content from masquerading
                # as a worker-protocol final result.
                emit({"provider": "codex", "record": event})
                confirmation = stream.accept(event)
                if confirmation is not None:
                    emit(confirmation)
            code = child.wait()
        emit(stream.result(interrupted=interrupted))
        return 0 if interrupted or (code == 0 and stream.terminal == "turn.completed") else 1
    except Exception as exc:
        emit({"event": "protocol_error"})
        print("Codex bridge failed: " + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 1
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()


if __name__ == "__main__":
    raise SystemExit(main())
