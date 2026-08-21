# Task 3 Report: Public API and Contract Integration

## Files changed

- `proteus/safety/__init__.py`: published the approved full-case suite, boundary result and
  observation types, six boundary oracles, and `implemented_case_families` from
  `proteus.safety`; private family builders remain unexported.
- `tests/test_safety_cases.py`: added the package-root import and structural
  `HarnessSafetyCaseSuite` contract test.
- `tests/test_safety_boundary.py`: added the package-root boundary import and literal pass/fail
  contract test.

## RED

Command:

```text
uv run pytest tests/test_safety_cases.py tests/test_safety_boundary.py -q
```

Result: collection failed as expected because `ModuleSafetyCaseSuite` and `BoundaryOracleResult`
were not yet exported from `proteus.safety`.

## GREEN

Focused command:

```text
uv run pytest tests/test_safety_cases.py tests/test_safety_boundary.py \
  tests/test_module_safety_taxonomy.py tests/test_harness_safety_evaluator.py \
  tests/test_harness_safety_runtime.py -q
uv run ruff check proteus/safety/__init__.py proteus/safety/cases.py \
  proteus/safety/boundary.py tests/test_safety_cases.py tests/test_safety_boundary.py
```

Result: `58 passed`; Ruff: `All checks passed!`.

Repository-wide regression check:

```text
uv run pytest tests/ -q
```

Result: `172 passed in 6.02s`.

## Self-review

- Only the ten approved Task 3 symbols were added to the public safety API.
- No private family builders, default provider, case-specific evaluator logic, behavior verdict
  for boundary oracles, compatibility layer, hash, or unrelated cleanup was added.
- Existing case and boundary semantics were unchanged; tests exercise the package-root imports,
  structural suite protocol, and deterministic boundary pass/fail behavior.
- `git diff --check` passed.

## Commit

`feat(safety): publish concrete safety case interfaces`
