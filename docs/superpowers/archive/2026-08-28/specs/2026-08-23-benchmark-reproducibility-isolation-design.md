# Benchmark Reproducibility and Shared Isolation Design

## Context

The HumanEval benchmark added in PR #16 reuses the parent/worker isolation shape from
MBPP. The design keeps held-out tests and prompt helpers in a trusted driver while each
candidate function executes in a separate worker process. Review found no blocking defect,
but identified two follow-ups before more benchmark packs are added:

1. automatic dataset downloads use moving `master` URLs without integrity verification;
2. the security-critical codec, worker transport, and driver proxy are substantially
   duplicated between HumanEval and MBPP.

This change fixes both without changing benchmark scoring semantics or expanding the public
API.

## Goals

- Make official HumanEval and MBPP downloads byte-for-byte reproducible.
- Reject corrupted or unexpectedly changed official downloads before they enter the cache.
- Keep explicit paths and `PROTEUS_HUMANEVAL_PATH` / `PROTEUS_MBPP_PATH` available for
  user-supplied datasets without imposing the official checksum.
- Give HumanEval and MBPP one shared implementation of value encoding, candidate worker
  execution, remote calls, random report channels, and failure handling.
- Preserve HumanEval's official `check(candidate)` behavior, including driver-side prompt
  helpers.
- Preserve MBPP's per-assertion partial scoring.
- Run the adversarial isolation contract against both benchmark packs.

## Non-goals

- No generic benchmark base class or plugin framework.
- No change to `BenchTask`, CLI flags, task IDs, cache locations, or evaluator result shape.
- No checksum enforcement for explicit or environment-provided dataset paths.
- No network fallback during grading.
- No attempt to make Python process isolation replace the existing networkless Docker
  grader; the shared harness remains defense in depth inside that container.

## Dataset Reproducibility

Each benchmark module will declare an official immutable raw URL containing a full upstream
commit SHA and a SHA-256 digest for the downloaded bytes. Automatic download follows this
sequence:

1. fetch bytes from the immutable URL;
2. compare `sha256(payload)` with the module's expected digest;
3. parse and validate the dataset using the benchmark's existing loader;
4. atomically replace the cache path only after both checks pass.

A checksum mismatch raises `ValueError` with the benchmark name, expected digest, and actual
digest. No cache file is published. An existing cache retains the current behavior: it is
used without a network request. Explicit function arguments and environment paths bypass
the official downloader and are parsed normally; they are intentionally user-controlled.

The small download primitive will live in `proteus/bench/_datasets.py` so both packs share
the ordering and failure semantics. It accepts the URL, expected digest, cache path, and a
validation callable; benchmark-specific parsing stays in the benchmark module.

## Shared Isolation Boundary

`proteus/bench/_isolation.py` will own only mechanics that are identical across benchmark
packs:

- the value codec source used on both sides of the process boundary;
- a candidate worker source that loads `solution.py`, selects a named entry point, executes
  one call, and emits one randomly-prefixed encoded response;
- the trusted driver support source that launches workers, validates their exit/report,
  decodes values, and exposes a named remote-function proxy;
- construction helpers that bind random prefixes, worker source, and call timeout into a
  self-contained grader script.

The request schema is normalized to `{name, args, kwargs}`. HumanEval supplies its single
official entry-point name on every call; MBPP continues to supply the function referenced by
each assertion. This removes the only material worker protocol difference.

Benchmark-owned driver bodies remain separate:

- HumanEval executes `PROMPT_SOURCE` in the trusted driver, replaces only the official entry
  point with the remote proxy, executes held-out `TEST_SOURCE`, and calls `check(candidate)`.
- MBPP installs remote proxies for every declared entry point, executes trusted imports and
  assertions, and reports the number passed.

Candidate code never receives prompt tests, reference imports, report prefixes, or driver
frames. Candidate stdout is not itself a grade; only the trusted driver emits the final
randomly-prefixed report consumed by the host evaluator.

## Failure Semantics

- A worker timeout, nonzero exit, malformed response, missing random-prefix report, or
  candidate exception becomes a failed candidate call.
- HumanEval maps any official check failure to score `0.0`; a complete official check maps
  to `1.0`.
- MBPP maps each failed assertion to zero credit for that assertion and preserves dense
  partial scoring for the remainder.
- A grader timeout or unavailable Docker sandbox retains the existing scored-failure
  behavior.
- The temporary `_grade.py` is deleted in all outcomes.

## Tests

Development follows red-green-refactor.

Dataset tests will prove:

- automatic downloads use immutable commit URLs;
- matching payloads validate, cache atomically, and are reused without another request;
- checksum mismatches raise and leave no cache file;
- explicit and environment paths bypass the official checksum while remaining parseable.

Isolation tests will be parameterized across HumanEval and MBPP where the attack applies:

- candidate attempts to forge a final grader report;
- candidate exits the worker early;
- candidate patches `builtins`, including `exec`;
- task-local modules attempt to shadow trusted grader imports;
- candidate attempts to reach trusted control data through stack frames;
- malformed or missing worker reports fail closed.

Existing benchmark-specific correctness tests remain: canonical solutions pass, seeded
stubs fail, HumanEval prompt helpers execute in the trusted driver, and MBPP partial scores
remain dense.

The final gate is the complete pytest suite, `tests/run_offline.py`, and Ruff. No live model,
Docker image download, or external network is required by the new tests.

## Compatibility and Rollout

The refactor preserves the public functions `dataset_path`, `list_tasks`, and task factory
signatures. Existing cache paths remain valid. Previously cached files are not retroactively
hashed because they may predate the pin and users may deliberately manage those caches; a
fresh automatic download is the integrity boundary introduced here.

The two changes will be implemented as independently testable commits: dataset pinning and
verification first, shared isolation second. This keeps review and rollback straightforward.
