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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping, Protocol


@dataclass(frozen=True)
class SandboxConfig:
    """User-tunable sandbox setup.

    Everything about the container is a variable, so evolving inside your own environment
    is a config change and not a fork: bring an image (any registry ref or local tag),
    declare what it needs (network, memory, cpus, mounts, env), and pass whatever else
    your setup requires straight through to `docker run` via `extra_args`.
    """

    network: str = "none"                 # "none" | "host" | "bridge" | a named network
    image: str = "proteus-episode:latest"
    mem_limit: str = ""                   # e.g. "4g"; "" = unlimited
    cpus: str = ""                        # e.g. "2"; "" = unlimited
    extra_mounts: tuple[tuple[str, str], ...] = ()   # (host_path, container_path) read-write
    env_passthrough: tuple[str, ...] = ()            # env var names to forward (e.g. API keys)
    env: Mapping[str, str] = field(default_factory=dict)
    """Literal values set in the container (config the image needs, not secrets — these
    land in the process table; put credentials in `env_passthrough` instead)."""
    entrypoint: tuple[str, ...] = ()      # override; adapter usually supplies the command
    workdir: str = ""                     # container working directory
    user: str = ""                        # e.g. "1000:1000" to avoid root-owned artefacts
    extra_args: tuple[str, ...] = ()
    """Verbatim `docker run` flags, inserted before the image — the escape hatch for
    anything this dataclass does not name (`--gpus all`, `--platform`, `--shm-size`, an
    extra `--volume`, a `--security-opt`)."""

    @classmethod
    def from_manifest(cls, path: Path) -> "SandboxConfig":
        """Load the `[environment]` table of an `environments/<name>/environment.toml`.

        `docker_image` (a registry ref) takes precedence over `image` (the local tag the
        directory's Dockerfile builds) — the same short-circuit Harbor's task config uses,
        so one manifest serves both the prebuilt and the build-it-yourself path.
        """
        try:
            import tomllib
        except ImportError:  # Python < 3.11
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError as exc:
                raise RuntimeError(
                    "reading an environment manifest needs Python 3.11+ or "
                    "`pip install tomli`; or pass the image reference directly"
                ) from exc
        env = tomllib.loads(Path(path).read_text(encoding="utf-8"))["environment"]
        mounts = tuple((str(m["host"]), str(m["container"]))
                       for m in env.get("mounts", ()) if m.get("host") and m.get("container"))
        return cls(
            network=env.get("network", "none"),
            image=env.get("docker_image") or env.get("image", "proteus-episode:latest"),
            mem_limit=str(env.get("memory", "")),
            cpus=str(env.get("cpus", "")) if env.get("cpus") else "",
            extra_mounts=mounts,
            env_passthrough=tuple(env.get("env_passthrough", ())),
            env={str(k): str(v) for k, v in (env.get("env") or {}).items()},
            entrypoint=tuple(env.get("entrypoint", ())),
            workdir=str(env.get("workdir", "")),
            user=str(env.get("user", "")),
            extra_args=tuple(str(a) for a in env.get("docker_args", ())),
        )

    @classmethod
    def from_spec(cls, spec: str, **overrides) -> "SandboxConfig":
        """Resolve what a user typed into a config: a manifest, a directory, or an image.

        - `path/to/environment.toml` or a directory holding one -> `from_manifest`
        - anything else is taken as an image reference (`ghcr.io/me/env:1.2`, `my-env:dev`)

        `overrides` are applied on top, so a CLI flag beats what the manifest declared.
        """
        p = Path(spec).expanduser()
        if p.is_dir():
            p = p / "environment.toml"
        base = cls.from_manifest(p) if p.is_file() else cls(image=spec)
        return replace(base, **{k: v for k, v in overrides.items() if v not in (None, "", ())})


class Sandbox(Protocol):
    def run(self, run_root: Path, command: list[str], env: Mapping[str, str],
            timeout_s: int, mounts: tuple[tuple[str, ...], ...] = (),
            stop_check: "Callable[[], bool] | None" = None,
            ) -> subprocess.CompletedProcess:
        """`stop_check`, when given, is polled while the command runs; returning True
        stops the container. The caller owns the semantics of a stop (a turn budget is
        a cap, not an error)."""
        ...


class LocalSandbox:
    """No isolation — runs the command as a plain subprocess. For trusted harnesses only."""

    def run(self, run_root: Path, command: list[str], env: Mapping[str, str],
            timeout_s: int, mounts: tuple[tuple[str, ...], ...] = (),
            stop_check=None,   # in-process harnesses enforce budgets natively; unused here
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
            timeout_s: int, mounts: tuple[tuple[str, ...], ...] = (),
            stop_check=None, poll_s: float = 2.0,
            ) -> subprocess.CompletedProcess:
        """`mounts` replaces the default `<run_root>:/run` bind when given — adapters with
        their own container layout (e.g. dsh's /workspace + /state) pass it per call.

        With `stop_check`, the container runs named and is polled every `poll_s`; when
        the callable returns True the container is killed and the process's own exit
        code is returned — the caller decides whether that stop was a cap or a failure.
        """
        from proteus.core.recovery import RecoveryRequired
        from proteus.core.episode import private_record_dir
        marker = private_record_dir(run_root) / "docker-cleanup.json"
        if marker.exists():
            raise RecoveryRequired("Docker cleanup remains unverified; reconcile_cleanup before dispatch")
        c = self.config
        import uuid

        name = f"proteus-{uuid.uuid4().hex[:12]}"
        argv = ["docker", "run", "--rm", "--init", "--network", c.network,
                "--name", name]
        docker_env = os.environ.copy()
        for mount in (mounts or ((str(run_root), "/run"),)):
            if len(mount) not in (2, 3):
                raise ValueError(f"mount must be (host, container[, options]), got {mount!r}")
            host, cont, *options = mount
            suffix = f":{options[0]}" if options else ""
            argv += ["-v", f"{host}:{cont}{suffix}"]
        if c.mem_limit:
            argv += ["--memory", c.mem_limit]
        if c.cpus:
            argv += ["--cpus", c.cpus]
        if c.workdir:
            argv += ["--workdir", c.workdir]
        if c.user:
            argv += ["--user", c.user]
        for host, cont in c.extra_mounts:
            argv += ["-v", f"{host}:{cont}"]
        for key in c.env_passthrough:
            if key in env:
                # Let Docker copy the value from its own environment.  Keeping the
                # value out of argv prevents API keys from appearing in `ps` output.
                argv += ["-e", key]
                docker_env[key] = env[key]
        for key, value in c.env.items():
            argv += ["-e", f"{key}={value}"]
        argv += [*c.extra_args, c.image, *(c.entrypoint or ()), *command]
        if stop_check is None:
            try:
                return subprocess.run(
                    argv, capture_output=True, text=True, errors="replace",
                    env=docker_env, timeout=timeout_s, check=False)
            except BaseException:
                self._cleanup(name, marker)
                raise

        import time
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", env=docker_env)
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout_s)
                try:
                    out, err = proc.communicate(timeout=min(poll_s, remaining))
                    return subprocess.CompletedProcess(argv, proc.returncode, out, err)
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(argv, timeout_s)
                if stop_check():
                    out, err = self._cleanup(name, marker, proc)
                    return subprocess.CompletedProcess(argv, proc.returncode, out, err)
        except RecoveryRequired:
            raise
        except BaseException:
            self._cleanup(name, marker, proc)
            raise

    CLEANUP_SECONDS = 10.0

    @classmethod
    def _cleanup(cls, name, marker, proc=None):
        """Bound container removal and client drain by one shared deadline."""
        import time
        from proteus.core.recovery import RecoveryRequired, _write
        deadline = time.monotonic() + cls.CLEANUP_SECONDS
        def remaining():
            return max(0.001, deadline - time.monotonic())
        try:
            removed = subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                                     text=True, timeout=remaining(), check=False)
            if proc is not None:
                if removed.returncode and proc.poll() is None:
                    proc.terminate()
                try:
                    out, err = proc.communicate(timeout=min(2.0, remaining()))
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate(timeout=remaining())
                if removed.returncode:
                    # Registration may have raced the first removal. The client is
                    # now stopped; removal must be checked again before proceeding.
                    removed = subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                                             text=True, timeout=remaining(), check=False)
            else:
                out, err = "", ""
            if removed.returncode and "No such container" not in (removed.stderr or ""):
                raise RuntimeError("Docker did not confirm container removal")
            return out, err
        except BaseException as exc:
            if proc is not None and proc.poll() is None:
                proc.kill()  # Never follow this with an unbounded pipe drain.
            _write(marker, {"version": 1, "container": name})
            raise RecoveryRequired(f"Docker cleanup unverified for {name}; reservation must remain active") from exc

    def reconcile_cleanup(self, run_root: Path):
        """Explicitly verify removal before permitting work in an uncertain root."""
        from proteus.core.episode import private_record_dir
        from proteus.core.recovery import _load
        from proteus.artifacts.store import _sync_directory
        marker = private_record_dir(run_root) / "docker-cleanup.json"
        if marker.exists():
            record = _load(marker)
            if record["version"] != 1 or not record["container"].startswith("proteus-"):
                raise ValueError("invalid Docker cleanup receipt")
            self._cleanup(record["container"], marker)
            marker.unlink()
            _sync_directory(marker.parent)
