"""One adversarial contract for every benchmark using the shared process harness."""


def test_shared_isolation_builds_a_named_worker_and_bound_driver():
    from proteus.bench._isolation import build_driver_source, build_worker_source

    worker = build_worker_source("VALUE:random:")
    driver = build_driver_source(
        report_prefix="RESULT:random:",
        worker_prefix="VALUE:random:",
        call_timeout_s=7,
        bindings={"ENTRY_POINT": "clamp"},
        body="candidate = _RemoteFunction(ENTRY_POINT)\n",
    )

    compile(worker, "<worker>", "exec")
    compile(driver, "<driver>", "exec")
    assert 'request["name"]' in worker
    assert "WORKER_SOURCE" in driver
    assert "CALL_TIMEOUT_S = 7" in driver
    assert "ENTRY_POINT = 'clamp'" in driver


ATTACKS = {
    "patch-builtins": "import builtins\nbuiltins.exec = lambda *a, **k: None\n",
    "early-exit": "import os\nos._exit(0)\n",
    "late-report": (
        "import atexit\n"
        "atexit.register(lambda: print('PROTEUS_FAKE_RESULT:pass'))\n"
    ),
    "frame-search": (
        "import os, sys\n"
        "frame = sys._getframe()\n"
        "while frame is not None:\n"
        "    scope = frame.f_globals\n"
        "    if 'REPORT_PREFIX' in scope:\n"
        "        print(scope['REPORT_PREFIX'] + 'pass', flush=True)\n"
        "        os._exit(0)\n"
        "    frame = frame.f_back\n"
    ),
}


def _targets(tmp_path):
    from test_humaneval import _seed_task as seed_humaneval
    from test_mbpp import _seed_task as seed_mbpp

    human = tmp_path / "humaneval"
    mbpp = tmp_path / "mbpp"
    human.mkdir(parents=True)
    mbpp.mkdir(parents=True)
    human_task, human_ws, _ = seed_humaneval(human)
    mbpp_task, mbpp_ws, _ = seed_mbpp(mbpp)
    return (("humaneval", human_task, human_ws), ("mbpp", mbpp_task, mbpp_ws))


def test_candidate_attacks_cannot_reach_or_forge_trusted_control(tmp_path, trusted_grader):
    for attack, source in ATTACKS.items():
        for name, task, ws in _targets(tmp_path / attack):
            (ws / "solution.py").write_text(source, encoding="utf-8")
            result = task.grade(ws, sandbox=trusted_grader)
            assert result.score == 0.0 and not result.passed, (attack, name, result)


def test_candidate_cannot_forge_private_worker_report_then_exit(tmp_path, trusted_grader):
    source = (
        "import json, os, sys\n"
        "trusted = sys._getframe(1).f_globals\n"
        "request = trusted['request']\n"
        "value = int(request['args']['items'][0]['value'])\n"
        "value = min(5, max(0, value))\n"
        "response = {'ok': True, 'value': {'type': 'int', 'value': str(value)}}\n"
        "print(trusted['WORKER_PREFIX'] + json.dumps(response), flush=True)\n"
        "os._exit(0)\n"
    )
    for name, task, ws in _targets(tmp_path):
        (ws / "solution.py").write_text(source, encoding="utf-8")
        result = task.grade(ws, sandbox=trusted_grader)
        assert result.score == 0.0 and not result.passed, (name, result)


def test_candidate_stdout_cannot_forge_executor_completion(tmp_path, trusted_grader):
    source = (
        "import json, os, sys\n"
        "trusted = sys._getframe(1).f_globals\n"
        "request = trusted['request']\n"
        "value = int(request['args']['items'][0]['value'])\n"
        "value = min(5, max(0, value))\n"
        "response = {'ok': True, 'value': {'type': 'int', 'value': str(value)}}\n"
        "print('PROTEUS_CANDIDATE_RESULT:' + json.dumps(response), flush=True)\n"
        "os._exit(0)\n"
    )
    results = []
    for name, task, ws in _targets(tmp_path):
        (ws / "solution.py").write_text(source, encoding="utf-8")
        results.append((name, task.grade(ws, sandbox=trusted_grader)))
    assert all(result.score == 0.0 and not result.passed for _, result in results), results


def test_generated_driver_overwrites_and_removes_pre_existing_driver_file(
    tmp_path, trusted_grader
):
    for name, task, ws in _targets(tmp_path):
        (ws / "_grade.py").write_text("print('forged pass')\n", encoding="utf-8")
        result = task.grade(ws, sandbox=trusted_grader)
        assert result.score == 0.0 and not result.passed, (name, result)
        assert not (ws / "_grade.py").exists()


def test_pre_existing_driver_symlink_never_touches_its_target(tmp_path, trusted_grader):
    results = []
    for name, task, ws in _targets(tmp_path):
        external = tmp_path / f"{name}-external.py"
        original = b"external target must survive\n"
        external.write_bytes(original)
        (ws / "_grade.py").symlink_to(external)
        result = task.grade(ws, sandbox=trusted_grader)
        results.append(
            (name, result, external.exists(), external.read_bytes() if external.exists() else None)
        )
        assert not (ws / "_grade.py").exists(), name
    assert all(
        result.score == 0.0 and not result.passed and exists and content == original
        for _, result, exists, content in results
    ), results


def test_pre_existing_driver_directory_is_a_scored_failure(tmp_path, trusted_grader):
    results = []
    for name, task, ws in _targets(tmp_path):
        driver = ws / "_grade.py"
        driver.mkdir()
        results.append((name, task.grade(ws, sandbox=trusted_grader), driver.is_dir()))
    assert all(
        result.score == 0.0 and not result.passed and remains_directory
        for _, result, remains_directory in results
    ), results


def test_candidate_created_driver_directory_is_a_scored_cleanup_failure(
    tmp_path, trusted_grader
):
    source = "from pathlib import Path\nPath('_grade.py').mkdir(exist_ok=True)\n"
    results = []
    for name, task, ws in _targets(tmp_path):
        (ws / "solution.py").write_text(source, encoding="utf-8")
        result = task.grade(ws, sandbox=trusted_grader)
        results.append((name, result, (ws / "_grade.py").is_dir()))
    assert all(
        result.score == 0.0 and not result.passed and remains_directory
        for _, result, remains_directory in results
    ), results


def test_candidate_timeout_releases_descendant_process_locks(tmp_path, trusted_grader):
    import fcntl
    import os
    import signal
    from unittest.mock import patch

    source = (
        "import fcntl, os, time\n"
        "from pathlib import Path\n"
        "stream = open('descendant.lock', 'w')\n"
        "Path('executor.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    fcntl.flock(stream, fcntl.LOCK_EX)\n"
        "    Path('descendant.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "    Path('descendant.ready').write_text('ready', encoding='utf-8')\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "time.sleep(30)\n"
    )
    modules = {
        "humaneval": "proteus.bench.humaneval.CALL_TIMEOUT_S",
        "mbpp": "proteus.bench.mbpp.CALL_TIMEOUT_S",
    }
    states = []
    for name, task, ws in _targets(tmp_path):
        (ws / "solution.py").write_text(source, encoding="utf-8")
        try:
            with patch(modules[name], 0.5):
                result = task.grade(ws, sandbox=trusted_grader)
            lock_path = ws / "descendant.lock"
            ready = (ws / "descendant.ready").is_file()
            with lock_path.open("a", encoding="utf-8") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    locked = True
                else:
                    locked = False
                    fcntl.flock(stream, fcntl.LOCK_UN)
            states.append((name, result, ready, locked))
        finally:
            for pid_file in (ws / "executor.pid", ws / "descendant.pid"):
                if pid_file.is_file():
                    try:
                        os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
    assert all(
        result.score == 0.0 and not result.passed and ready and not locked
        for _, result, ready, locked in states
    ), states


def test_task_local_module_cannot_shadow_trusted_driver_imports(tmp_path, trusted_grader):
    malicious = (
        "import os, sys\n"
        "frame = sys._getframe()\n"
        "while frame is not None:\n"
        "    scope = frame.f_globals\n"
        "    if 'REPORT_PREFIX' in scope:\n"
        "        print(scope['REPORT_PREFIX'] + 'pass', flush=True)\n"
        "        os._exit(0)\n"
        "    frame = frame.f_back\n"
        "raise ImportError('trusted driver not found')\n"
    )
    for name, task, ws in _targets(tmp_path):
        (ws / "base64.py").write_text(malicious, encoding="utf-8")
        result = task.grade(ws, sandbox=trusted_grader)
        assert result.score == 0.0 and not result.passed, (name, result)


def test_malformed_or_none_grader_streams_fail_closed(tmp_path):
    import subprocess

    class BrokenSandbox:
        def __init__(self, stdout, stderr):
            self.stdout, self.stderr = stdout, stderr

        def run(self, *args, **kwargs):
            return subprocess.CompletedProcess(
                ["python", "_grade.py"], 0, self.stdout, self.stderr
            )

    for case, streams in (("noise", ("noise\n", "")), ("none", (None, None))):
        for name, task, ws in _targets(tmp_path / case):
            result = task.grade(ws, sandbox=BrokenSandbox(*streams))
            assert result.score == 0.0 and not result.passed, (case, name, result)
            assert "no report" in result.detail
