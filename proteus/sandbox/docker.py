"""Sandboxing for the episode process.

A self-evolving agent writes and runs its own code, so an application-level file sandbox
cannot contain it — only OS-level isolation can. Proteus therefore runs each episode inside
a user-configurable sandbox. The minimal reference harness is trusted and can use
`LocalSandbox` (in-process); any real self-editing harness should use `DockerSandbox`,
whose container filesystem contains the harness and nothing else (no host repo, no study).

`SandboxConfig` is what the user tunes — network on/off is the important one for
containment (a harness with no network cannot exfiltrate; a harness that needs an LLM
endpoint needs egress).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SandboxConfig:
    """User-tunable sandbox setup."""

    network: str = "none"                 # "none" | "host" | "bridge"
    image: str = "proteus-episode:latest"
    mem_limit: str = ""                   # e.g. "4g"; "" = unlimited
    cpus: str = ""                        # e.g. "2"; "" = unlimited
    extra_mounts: tuple[tuple[str, str], ...] = ()   # (host_path, container_path) read-write
    env_passthrough: tuple[str, ...] = ()            # env var names to forward (e.g. API keys)
    entrypoint: tuple[str, ...] = ()      # override; adapter usually supplies the command

    @classmethod
    def from_manifest(cls, path: Path) -> SandboxConfig:
        """Load the `[environment]` table of an `environments/<name>/environment.toml`.

        `docker_image` (a registry ref) takes precedence over `image` (the local tag the
        directory's Dockerfile builds) — the same short-circuit Harbor's task config uses,
        so one manifest serves both the prebuilt and the build-it-yourself path.
        """
        try:
            import tomllib
        except ImportError:  # Python 3.10
            import tomli as tomllib  # type: ignore[no-redef]
        env = tomllib.loads(Path(path).read_text(encoding="utf-8"))["environment"]
        return cls(
            network=env.get("network", "none"),
            image=env.get("docker_image") or env.get("image", "proteus-episode:latest"),
            mem_limit=str(env.get("memory", "")),
            cpus=str(env.get("cpus", "")) if env.get("cpus") else "",
            env_passthrough=tuple(env.get("env_passthrough", ())),
        )


class Sandbox(Protocol):
    def run(self, run_root: Path, command: list[str], env: Mapping[str, str],
            timeout_s: int, mounts: tuple[tuple[str, str], ...] = ()
            ) -> subprocess.CompletedProcess:
        ...


class LocalSandbox:
    """No isolation — runs the command as a plain subprocess. For trusted harnesses only."""

    def run(self, run_root: Path, command: list[str], env: Mapping[str, str],
            timeout_s: int, mounts: tuple[tuple[str, str], ...] = ()
            ) -> subprocess.CompletedProcess:
        return subprocess.run(command, capture_output=True, text=True, cwd=str(run_root),
                              env={**dict(env)}, timeout=timeout_s, check=False)


class DockerSandbox:
    """OS-level isolation. Mounts only the run root; the host filesystem is otherwise absent.

    Mirrors the containment used in the research runner: `--network` is configurable
    (default `none`; use `host` when the harness must reach an LLM endpoint and the
    platform's NAT is unreliable under load), the run root is the only bind mount, and only
    named env vars cross in.
    """

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    def run(self, run_root: Path, command: list[str], env: Mapping[str, str],
            timeout_s: int, mounts: tuple[tuple[str, str], ...] = ()
            ) -> subprocess.CompletedProcess:
        """`mounts` replaces the default `<run_root>:/run` bind when given — adapters with
        their own container layout (e.g. dsh's /workspace + /state) pass it per call."""
        c = self.config
        argv = ["docker", "run", "--rm", "--init", "--network", c.network]
        for host, cont in (mounts or ((str(run_root), "/run"),)):
            argv += ["-v", f"{host}:{cont}"]
        if c.mem_limit:
            argv += ["--memory", c.mem_limit]
        if c.cpus:
            argv += ["--cpus", c.cpus]
        for host, cont in c.extra_mounts:
            argv += ["-v", f"{host}:{cont}"]
        for key in c.env_passthrough:
            if key in env:
                argv += ["-e", key]
        argv += [c.image, *(c.entrypoint or ()), *command]
        client_env = os.environ.copy()
        client_env.update({key: env[key] for key in c.env_passthrough if key in env})
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=client_env,
            check=False,
        )
