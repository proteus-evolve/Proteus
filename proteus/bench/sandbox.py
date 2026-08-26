"""Contained execution for benchmark code authored by the evolving agent."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping


def default_grader_sandbox():
    """A small, networkless Python container with no host mounts except the task."""
    from proteus.sandbox import DockerSandbox, SandboxConfig

    user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else ""
    return DockerSandbox(SandboxConfig(
        image=os.environ.get("PROTEUS_GRADER_IMAGE", "python:3.12-slim"),
        network="none", workdir="/task", user=user, mem_limit="512m", cpus="1",
        env_passthrough=("PROTEUS_TEST_FILES",),
        extra_args=("--pids-limit", "128", "--read-only", "--tmpfs", "/tmp:size=64m"),
    ))


def run_python(ws: Path, script: str, *, timeout_s: int,
               env: Mapping[str, str] | None = None, sandbox=None,
               isolated: bool = False) -> subprocess.CompletedProcess:
    """Execute a task-local script without ever falling back to host Python."""
    runner = sandbox or default_grader_sandbox()
    command = ["python"]
    if isolated:
        command.append("-I")
    command.append(script)
    try:
        return runner.run(
            Path(ws), command, env=dict(env or {}), timeout_s=timeout_s,
            mounts=((str(Path(ws)), "/task"),),
        )
    except subprocess.TimeoutExpired:
        raise
    except Exception as exc:  # noqa: BLE001 - unavailable isolation is a scored failure
        return subprocess.CompletedProcess(
            command, 126, "",
            f"secure grader unavailable ({type(exc).__name__}: {exc})",
        )
