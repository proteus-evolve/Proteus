"""Test-only fixtures. Production benchmark code never falls back to host execution."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def make_trusted_grader():
    class TrustedTestSandbox:
        def run(self, run_root, command, env, timeout_s, mounts=(), **kwargs):
            # Executes only repository-owned fixture code in the test process's temp dir.
            host = next(Path(src) for src, dest in mounts if dest == "/task")
            return subprocess.run(
                [sys.executable, *command[1:]], cwd=host,
                env={**os.environ, **env}, capture_output=True, text=True,
                timeout=timeout_s, check=False,
            )

    return TrustedTestSandbox()


@pytest.fixture
def trusted_grader():
    return make_trusted_grader()
