"""Offline test runner for environments without pytest. Exit code = failures."""
import pathlib
import inspect
import os
import sys
import subprocess
import tempfile
import traceback

TEST_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR.parent))
sys.path.insert(0, str(TEST_DIR))


def make_trusted_grader():
    """Host runner for repository-owned temp fixtures; never used by production code."""
    class TrustedTestSandbox:
        def run(self, run_root, command, env, timeout_s, mounts=(), **kwargs):
            host = next(pathlib.Path(src) for src, dest in mounts if dest == "/task")
            return subprocess.run(
                [sys.executable, *command[1:]], cwd=host,
                env={**os.environ, **env}, capture_output=True, text=True,
                timeout=timeout_s, check=False)

    return TrustedTestSandbox()


def main() -> int:
    import test_aki_adapter as A
    import test_bench as B
    import test_goals as G
    import test_humaneval as H
    import test_instrument as I
    import test_mbpp as M
    import test_polyglot as P
    import test_selfcode as C
    import test_smoke as S
    tmp = pathlib.Path(tempfile.mkdtemp())
    passed = failed = 0
    for mod in (G, S, A, B, H, I, M, P, C):
        for name in [n for n in dir(mod) if n.startswith("test_")]:
            fn = getattr(mod, name)
            d = tmp / mod.__name__ / name
            d.mkdir(parents=True)
            try:
                fixtures = {
                    "tmp_path": d,
                    "trusted_grader": make_trusted_grader(),
                }
                params = inspect.signature(fn).parameters
                unknown = set(params) - set(fixtures)
                if unknown:
                    raise RuntimeError(f"offline runner has no fixture(s): {sorted(unknown)}")
                fn(**{key: fixtures[key] for key in params})
                passed += 1
            except Exception:  # noqa: BLE001 - a runner reports failures, it does not raise
                print(f"FAIL {name}")
                traceback.print_exc(limit=3)
                failed += 1
    print(f"{passed} passed, {failed} failed")
    return failed

if __name__ == "__main__":
    sys.exit(main())
