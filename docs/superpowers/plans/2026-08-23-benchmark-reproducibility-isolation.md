# Benchmark Reproducibility and Shared Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pin and verify the official HumanEval/MBPP datasets, then replace their duplicated security-critical process harness with one shared isolation module without changing scoring behavior.

**Architecture:** A focused `_datasets.py` owns verified atomic downloads while each benchmark retains its parser and user-supplied path policy. A focused `_isolation.py` owns the common value codec, named candidate worker, trusted remote-call proxy, and source binding; HumanEval and MBPP retain separate driver bodies for their distinct official-check and partial-score semantics.

**Tech Stack:** Python 3.10+, stdlib (`hashlib`, `tempfile`, `urllib`, `subprocess`, `json`), pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-23-benchmark-reproducibility-isolation-design.md`

## Global Constraints

- Preserve Python 3.10 support and the package's stdlib-only core dependency contract.
- Preserve `dataset_path`, `list_tasks`, `humaneval_task`, and `mbpp_task` signatures.
- Preserve HumanEval binary official-check scoring and driver-side prompt helpers.
- Preserve MBPP per-assertion dense partial scoring.
- Verify only automatic official downloads; explicit and environment-provided paths remain user-supplied and unverified.
- Keep grading networkless and fail closed on missing, malformed, forged, timed-out, or nonzero worker reports.
- Do not introduce a generic benchmark base class.

---

### Task 1: Immutable, Verified Official Dataset Downloads

**Files:**
- Create: `proteus/bench/_datasets.py`
- Modify: `proteus/bench/humaneval.py:9-23,197-221`
- Modify: `proteus/bench/mbpp.py:13-28,215-240`
- Modify: `tests/test_humaneval.py:63-90`
- Modify: `tests/test_mbpp.py:56-81`
- Test: `tests/test_datasets.py`
- Modify: `tests/run_offline.py:28-40`

**Interfaces:**
- Produces: `download_verified(*, name: str, url: str, expected_sha256: str, cache: Path, validate: Callable[[Path], object]) -> Path`.
- Consumes: benchmark-local `_records(path)` functions as validation callbacks.
- Produces constants:
  - HumanEval URL commit `6d43fb980f9fee3c892a914eda09951f772ad10d` and SHA-256 `b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef`.
  - MBPP URL commit `e20eb00d074cdb569ee27318f112ea1e85bbb98f` and SHA-256 `ca95deaa9a01ef0a6f439f88bcf0dd3db3563d22f22aad6cae04ebb9a8d8c8e9`.

- [ ] **Step 1: Add failing tests for the shared verified-download contract**

Create `tests/test_datasets.py` with real byte payloads and a patched stdlib response:

```python
"""Verified official benchmark download behavior, fully offline."""

import hashlib
import io
from unittest.mock import patch


def test_verified_download_caches_only_a_matching_valid_payload(tmp_path):
    from proteus.bench._datasets import download_verified

    payload = b'[{"task_id": 1}]'
    cache = tmp_path / "dataset.json"
    validated = []

    with patch("proteus.bench._datasets.request.urlopen", return_value=io.BytesIO(payload)) as get:
        first = download_verified(
            name="fixture",
            url="https://example.invalid/pinned.json",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            cache=cache,
            validate=lambda path: validated.append(path.read_bytes()),
        )
        second = download_verified(
            name="fixture",
            url="https://example.invalid/pinned.json",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            cache=cache,
            validate=lambda path: validated.append(path.read_bytes()),
        )

    assert first == second == cache
    assert cache.read_bytes() == payload
    assert validated == [payload]
    assert get.call_count == 1


def test_verified_download_rejects_a_checksum_mismatch_without_publishing_cache(tmp_path):
    from proteus.bench._datasets import download_verified

    cache = tmp_path / "dataset.json"
    with patch("proteus.bench._datasets.request.urlopen", return_value=io.BytesIO(b"changed")):
        try:
            download_verified(
                name="fixture",
                url="https://example.invalid/pinned.json",
                expected_sha256="0" * 64,
                cache=cache,
                validate=lambda path: None,
            )
        except ValueError as exc:
            assert "fixture checksum mismatch" in str(exc)
            assert "expected" in str(exc) and "got" in str(exc)
        else:
            raise AssertionError("mismatched download was accepted")

    assert not cache.exists()
    assert list(tmp_path.iterdir()) == []


def test_official_dataset_urls_and_hashes_are_immutable():
    from proteus.bench import humaneval, mbpp

    assert "6d43fb980f9fee3c892a914eda09951f772ad10d" in humaneval.DATA_URL
    assert humaneval.DATA_SHA256 == "b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef"
    assert "e20eb00d074cdb569ee27318f112ea1e85bbb98f" in mbpp.DATA_URL
    assert mbpp.DATA_SHA256 == "ca95deaa9a01ef0a6f439f88bcf0dd3db3563d22f22aad6cae04ebb9a8d8c8e9"
    assert "/master/" not in humaneval.DATA_URL
    assert "/master/" not in mbpp.DATA_URL
```

- [ ] **Step 2: Register the new test module in the no-pytest runner**

Modify `tests/run_offline.py`:

```python
import test_datasets as D
```

and include `D` in the module tuple:

```python
for mod in (G, S, A, B, D, H, I, M, P, C):
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_datasets.py -q
```

Expected: FAIL because `proteus.bench._datasets` and `DATA_SHA256` do not exist.

- [ ] **Step 4: Implement the minimal verified downloader**

Create `proteus/bench/_datasets.py`:

```python
"""Verified, atomic downloads for official benchmark datasets."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Callable
from urllib import request


def download_verified(*, name: str, url: str, expected_sha256: str, cache: Path,
                      validate: Callable[[Path], object]) -> Path:
    """Download one official dataset, verify bytes and format, then publish atomically."""
    cache = Path(cache)
    if cache.exists():
        return cache
    cache.parent.mkdir(parents=True, exist_ok=True)
    with request.urlopen(url, timeout=30) as response:
        payload = response.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"{name} checksum mismatch: expected {expected_sha256}, got {actual}"
        )
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache.parent, suffix="".join(cache.suffixes), delete=False
        ) as stream:
            stream.write(payload)
            temporary = Path(stream.name)
        validate(temporary)
        temporary.replace(cache)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return cache
```

- [ ] **Step 5: Verify the shared downloader tests are GREEN**

Run:

```bash
.venv/bin/pytest tests/test_datasets.py -q
```

Expected: the behavioral tests pass; the immutable-constant test still fails because the benchmark modules are not pinned yet.

- [ ] **Step 6: Pin HumanEval and route only its automatic path through the helper**

In `proteus/bench/humaneval.py`, replace the moving URL and local download block with:

```python
from proteus.bench._datasets import download_verified

DATA_URL = (
    "https://raw.githubusercontent.com/openai/human-eval/"
    "6d43fb980f9fee3c892a914eda09951f772ad10d/data/HumanEval.jsonl.gz"
)
DATA_SHA256 = "b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef"
```

The automatic branch becomes:

```python
cache = Path.home() / ".cache" / "proteus" / "humaneval" / "HumanEval.jsonl.gz"
return download_verified(
    name="HumanEval",
    url=DATA_URL,
    expected_sha256=DATA_SHA256,
    cache=cache,
    validate=_records,
)
```

Remove now-unused `tempfile` and `urllib.request` imports. Keep the explicit argument and environment branches before this call so they bypass official checksum enforcement.

- [ ] **Step 7: Pin MBPP and route only its automatic path through the helper**

In `proteus/bench/mbpp.py`, use:

```python
from proteus.bench._datasets import download_verified

DATA_URL = (
    "https://raw.githubusercontent.com/google-research/google-research/"
    "e20eb00d074cdb569ee27318f112ea1e85bbb98f/mbpp/sanitized-mbpp.json"
)
DATA_SHA256 = "ca95deaa9a01ef0a6f439f88bcf0dd3db3563d22f22aad6cae04ebb9a8d8c8e9"
```

The automatic branch becomes:

```python
cache = Path.home() / ".cache" / "proteus" / "mbpp" / "sanitized-mbpp.json"
return download_verified(
    name="MBPP",
    url=DATA_URL,
    expected_sha256=DATA_SHA256,
    cache=cache,
    validate=_records,
)
```

Remove now-unused `tempfile` and `urllib.request` imports.

- [ ] **Step 8: Update benchmark cache tests without allowing real network**

In both existing `test_default_dataset_downloads_to_cache_once` tests, patch the shared
request path and temporarily replace the production digest with the fixture digest:

```python
digest = hashlib.sha256(payload).hexdigest()
with patch("proteus.bench.humaneval.DATA_SHA256", digest), patch(
    "proteus.bench._datasets.request.urlopen", return_value=io.BytesIO(payload)
) as get:
    first = dataset_path()
    second = dataset_path()
```

Use the corresponding `proteus.bench.mbpp.DATA_SHA256` patch for MBPP. Add explicit-path and
environment-path tests that patch the module-local `download_verified` to raise if called,
then assert the supplied fixture is returned. These prove the intentional bypass contract.

Use this exact test shape in `tests/test_humaneval.py`, then repeat it with the MBPP fixture,
environment variable, and module path in `tests/test_mbpp.py`:

```python
def test_user_supplied_dataset_paths_bypass_official_verification(tmp_path):
    from unittest.mock import patch

    from proteus.bench import humaneval

    dataset, _ = _mini_dataset(tmp_path)
    with patch.object(
        humaneval, "download_verified", side_effect=AssertionError("official download called")
    ):
        assert humaneval.dataset_path(dataset) == dataset

        old = os.environ.get("PROTEUS_HUMANEVAL_PATH")
        os.environ["PROTEUS_HUMANEVAL_PATH"] = str(dataset)
        try:
            assert humaneval.dataset_path() == dataset
        finally:
            if old is None:
                os.environ.pop("PROTEUS_HUMANEVAL_PATH", None)
            else:
                os.environ["PROTEUS_HUMANEVAL_PATH"] = old
```

- [ ] **Step 9: Run the dataset and benchmark tests**

Run:

```bash
.venv/bin/pytest tests/test_datasets.py tests/test_humaneval.py tests/test_mbpp.py -q
```

Expected: all tests pass with no network access.

- [ ] **Step 10: Run the offline runner and Ruff for this commit**

Run:

```bash
.venv/bin/python tests/run_offline.py
.venv/bin/ruff check proteus/bench/_datasets.py proteus/bench/humaneval.py proteus/bench/mbpp.py tests/test_datasets.py tests/test_humaneval.py tests/test_mbpp.py tests/run_offline.py
git diff --check
```

Expected: zero failures and no lint or whitespace errors.

- [ ] **Step 11: Commit the dataset change**

```bash
git add proteus/bench/_datasets.py proteus/bench/humaneval.py proteus/bench/mbpp.py \
  tests/test_datasets.py tests/test_humaneval.py tests/test_mbpp.py tests/run_offline.py
git commit -m "fix: pin and verify benchmark datasets"
```

---

### Task 2: Shared Parent/Worker Isolation Harness

**Files:**
- Create: `proteus/bench/_isolation.py`
- Modify: `proteus/bench/humaneval.py:27-194,257-290`
- Modify: `proteus/bench/mbpp.py:32-212,279-327`
- Create: `tests/test_benchmark_isolation.py`
- Modify: `tests/test_humaneval.py:109-126`
- Modify: `tests/test_mbpp.py:156-259`
- Modify: `tests/run_offline.py:28-40`

**Interfaces:**
- Produces: `build_worker_source(worker_prefix: str) -> str`.
- Produces: `build_driver_source(*, report_prefix: str, worker_prefix: str, call_timeout_s: int, bindings: Mapping[str, object], body: str) -> str`.
- The shared worker request schema is `{"name": str, "args": encoded, "kwargs": encoded}`.
- HumanEval supplies `ENTRY_POINT` to `_RemoteFunction(ENTRY_POINT)` in its private body.
- MBPP supplies each value in `ENTRY_POINTS` to `_RemoteFunction(name)` in its private body.

- [ ] **Step 1: Add the failing shared-module contract test**

Create `tests/test_benchmark_isolation.py` beginning with:

```python
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
```

- [ ] **Step 2: Add one cross-benchmark adversarial suite before refactoring**

In the same file, create `_targets(tmp_path)` from the existing fabricated HumanEval and
MBPP fixtures. Each yielded value is `(name, task, workspace)` with its own directory. Add
plain test functions that loop over both targets and assert score zero for these candidate
actions:

```python
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
```

Use concrete target construction rather than a pytest parametrization so the no-pytest runner
can execute the same suite:

```python
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


def test_candidate_cannot_replace_the_driver_file(tmp_path, trusted_grader):
    for name, task, ws in _targets(tmp_path):
        (ws / "_grade.py").write_text("print('forged pass')\n", encoding="utf-8")
        result = task.grade(ws, sandbox=trusted_grader)
        assert result.score == 0.0 and not result.passed, (name, result)
        assert not (ws / "_grade.py").exists()
```

Add the task-local shadow attempt with the same frame-search body currently used by MBPP:

```python
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
```

Finally add shared malformed-stream cases:

```python
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
```

- [ ] **Step 3: Register the shared adversarial module in the offline runner**

Modify `tests/run_offline.py`:

```python
import test_benchmark_isolation as Q
```

and include `Q` once in the test-module tuple.

- [ ] **Step 4: Run the shared module test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_benchmark_isolation.py::test_shared_isolation_builds_a_named_worker_and_bound_driver -q
```

Expected: FAIL because `proteus.bench._isolation` does not exist.

- [ ] **Step 5: Implement the shared isolation source builders**

Create `proteus/bench/_isolation.py` with the existing codec moved verbatim once, followed by
a generic named worker and driver support. The public construction surface is:

```python
from __future__ import annotations

from typing import Mapping


def build_worker_source(worker_prefix: str) -> str:
    """Return a self-contained candidate worker with a private result prefix."""
    return f"WORKER_PREFIX = {worker_prefix!r}\n" + WORKER_SOURCE


def build_driver_source(*, report_prefix: str, worker_prefix: str,
                        call_timeout_s: int, bindings: Mapping[str, object],
                        body: str) -> str:
    """Bind trusted values around the common remote-call support and benchmark body."""
    worker = build_worker_source(worker_prefix)
    values = {
        **dict(bindings),
        "REPORT_PREFIX": report_prefix,
        "WORKER_PREFIX": worker_prefix,
        "WORKER_SOURCE": worker,
        "CALL_TIMEOUT_S": call_timeout_s,
    }
    header = "".join(f"{name} = {value!r}\n" for name, value in values.items())
    return header + DRIVER_SUPPORT_SOURCE + body
```

The shared worker must use the normalized request name:

```python
request = json_loads(sys.argv[1])
source = solution.read_text(encoding="utf-8")
trusted_exec(trusted_compile(source, str(solution), "exec"), namespace)
function = namespace[request["name"]]
value = function(*_decode(request["args"]), **_decode(request["kwargs"]))
```

The shared driver proxy must send that name and fail explicitly when no private worker report
exists:

```python
class _RemoteFunction:
    def __init__(self, name):
        self.name = name

    def __call__(self, *args, **kwargs):
        return _remote_call(self.name, args, kwargs)
```

- [ ] **Step 6: Verify the shared source-builder test is GREEN**

Run:

```bash
.venv/bin/pytest tests/test_benchmark_isolation.py::test_shared_isolation_builds_a_named_worker_and_bound_driver -q
```

Expected: PASS.

- [ ] **Step 7: Migrate HumanEval while retaining its private official-check body**

Replace HumanEval's `_CODEC`, `_WORKER`, and common `_DRIVER` support with imports:

```python
from proteus.bench._isolation import build_driver_source
```

Keep a HumanEval-only `_DRIVER_BODY`:

```python
_DRIVER_BODY = '''\
try:
    Path(__file__).resolve().unlink()
    namespace = {"__name__": "__main__"}
    exec(PROMPT_SOURCE, namespace)
    candidate = _RemoteFunction(ENTRY_POINT)
    namespace[ENTRY_POINT] = candidate
    exec(TEST_SOURCE, namespace)
    namespace["check"](candidate)
    passed = True
except caught:
    passed = False
emit(REPORT_PREFIX + ("pass" if passed else "fail") + "\\n")
flush()
exit_now(0)
'''
```

Build `_grade.py` with:

```python
driver.write_text(
    build_driver_source(
        report_prefix=report_prefix,
        worker_prefix=worker_prefix,
        call_timeout_s=CALL_TIMEOUT_S,
        bindings={
            "PROMPT_SOURCE": spec["prompt"],
            "TEST_SOURCE": spec["test"],
            "ENTRY_POINT": spec["entry_point"],
        },
        body=_DRIVER_BODY,
    ),
    encoding="utf-8",
)
```

- [ ] **Step 8: Run HumanEval correctness and shared adversarial tests**

Run:

```bash
.venv/bin/pytest tests/test_humaneval.py tests/test_benchmark_isolation.py -q
```

Expected: HumanEval's stub still fails, canonical solution and prompt helper pass, and all
shared HumanEval attack cases fail closed.

- [ ] **Step 9: Migrate MBPP while retaining its private partial-score body**

Replace MBPP's duplicate codec/worker/driver support with `build_driver_source`. Keep its
existing trusted import, proxy installation, assertion loop, and `_report(passed)` logic in
an MBPP-only `_DRIVER_BODY`. Bind `TESTS`, `TEST_IMPORTS`, `REFERENCE_IMPORTS`, and
`ENTRY_POINTS` through the `bindings` mapping.

The complete benchmark-owned body is the current assertion loop without the shared imports
and proxy definition:

```python
_DRIVER_BODY = '''\
def _report(passed):
    emit(REPORT_PREFIX + str(passed) + "/" + str(len(TESTS)) + "\\n")
    flush()
    exit_now(0)

try:
    Path(__file__).resolve().unlink()
except OSError:
    _report(0)

namespace = {"__name__": "__main__"}
try:
    for statement in TEST_IMPORTS + REFERENCE_IMPORTS:
        exec(statement, namespace)
    for name in ENTRY_POINTS:
        namespace[name] = _RemoteFunction(name)
except caught:
    _report(0)

passed = 0
for assertion in TESTS:
    try:
        exec(assertion, namespace)
        passed += 1
    except caught:
        pass
_report(passed)
'''
```

Replace `_driver` with:

```python
def _driver(spec: dict, report_prefix: str, worker_prefix: str) -> str:
    return build_driver_source(
        report_prefix=report_prefix,
        worker_prefix=worker_prefix,
        call_timeout_s=CALL_TIMEOUT_S,
        bindings={
            "TEST_IMPORTS": list(spec.get("test_imports", [])),
            "REFERENCE_IMPORTS": _reference_imports(spec),
            "TESTS": list(spec["test_list"]),
            "ENTRY_POINTS": _entry_points(spec),
        },
        body=_DRIVER_BODY,
    )
```

The candidate worker now always receives the common `{name, args, kwargs}` request, which is
the schema MBPP already uses.

- [ ] **Step 10: Run both benchmark suites before removing duplicate attack tests**

Run:

```bash
.venv/bin/pytest tests/test_humaneval.py tests/test_mbpp.py tests/test_benchmark_isolation.py -q
```

Expected: all old benchmark tests and all new shared tests pass.

- [ ] **Step 11: Consolidate the adversarial tests into the shared suite**

Remove only the cases now exercised for both packs from their old locations:

- HumanEval: `test_candidate_cannot_reach_trusted_control_through_frames`.
- MBPP: grader replacement, patched `exec`, frame traversal, module shadowing, forged late
  report, early exit, malformed report, and `None` streams.

Keep benchmark-specific functional, partial-score, import, opaque-value, unavailable-sandbox,
and CLI tests in their original modules. Re-run the same three test files after removal to
prove consolidation did not reduce the contract.

- [ ] **Step 12: Run the complete verification gate**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/python tests/run_offline.py
.venv/bin/ruff check .
git diff --check
```

Expected: all pytest and offline tests pass, the one opt-in conformance test may remain
skipped, Ruff reports no findings, and Git reports no whitespace errors.

- [ ] **Step 13: Inspect the duplication removal and working-tree scope**

Run:

```bash
rg -n '^_CODEC|^_WORKER|^_DRIVER' proteus/bench/humaneval.py proteus/bench/mbpp.py proteus/bench/_isolation.py
git status --short
git diff --stat HEAD~1
```

Expected: the common codec/worker/driver support appears only in `_isolation.py`; benchmark
modules retain only `_DRIVER_BODY`; changed files are limited to this plan's production,
test, and documentation scope.

- [ ] **Step 14: Commit the shared-isolation change**

```bash
git add proteus/bench/_isolation.py proteus/bench/humaneval.py proteus/bench/mbpp.py \
  tests/test_benchmark_isolation.py tests/test_humaneval.py tests/test_mbpp.py \
  tests/run_offline.py
git commit -m "refactor: share benchmark isolation harness"
```
