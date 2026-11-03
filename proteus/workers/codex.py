"""Build a pinned Codex CLI bridge using the common persistent command runtime."""

import hashlib
from pathlib import Path
import sys
from urllib.parse import urlsplit

from proteus.core.contracts import require
from proteus.core.serialization import canonical_json
from proteus.workers.records import CommandWorkerOptions


def codex_worker(command, *, model, command_files, credential_env=("CODEX_API_KEY",),
                 api_base_url=None, api_key_env="CODEX_API_KEY", network_access=False,
                 timeout_seconds=3600, shutdown_grace_seconds=15):
    """Declare provider inputs without reading credentials or making a model call.

    ``command_files`` pins the executable and any wrapper scripts. The returned
    command options can be used by any method, not only the CORAL controller.
    This local backend remains trusted; its CLI sandbox is not a private-grader
    read boundary. Model-call and monetary usage remain unknown.
    """
    require(type(model) is str and bool(model.strip()), "Codex worker needs an explicit model")
    require(type(network_access) is bool, "Codex network access must be explicit")
    if api_base_url is not None:
        require(isinstance(api_base_url, str), "Codex API URL must be text")
        parsed = urlsplit(api_base_url)
        require(parsed.scheme in ("http", "https") and bool(parsed.netloc)
                and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment,
                "Codex API URL must not contain credentials, query or fragment")
        require(api_key_env in credential_env, "Codex API key environment must be explicitly declared")
    provider = CommandWorkerOptions(command=tuple(command), command_files=command_files,
        credential_env=tuple(credential_env), timeout_seconds=timeout_seconds,
        shutdown_grace_seconds=shutdown_grace_seconds)
    bridge = str(Path(__file__).with_name("codex_bridge.py").resolve())
    python = str(Path(sys.executable).absolute())
    pins = dict(command_files)
    for name in (python, bridge):
        pins[name] = "sha256:" + hashlib.sha256(Path(name).read_bytes()).hexdigest()
    spec = {"command": provider.command, "command_files": dict(command_files), "model": model,
            "network_access": network_access, "api_base_url": api_base_url, "api_key_env": api_key_env}
    return CommandWorkerOptions(command=(python, bridge, "--spec", canonical_json(spec)), command_files=pins,
        credential_env=provider.credential_env, timeout_seconds=timeout_seconds,
        shutdown_grace_seconds=shutdown_grace_seconds)
