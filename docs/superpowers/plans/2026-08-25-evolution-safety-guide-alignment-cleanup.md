# Evolution-Safety Guide Alignment Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align online candidate activation with the approved guide, make Aki/DSH protocol ownership explicit, and remove the superseded public safety-evaluator path.

**Architecture:** Keep `HarnessAdapter` for ordinary evolution and keep `CandidateSafetyAdapter` / `CandidateSafetyExecutor` as the optional structural safety extension consumed by `GateRunner`. Fix behavior at the existing controller and adapter boundaries, delete obsolete APIs without aliases, and retain unsupported native evidence as fail-closed missingness.

**Tech Stack:** Python 3.10+, dataclasses, runtime-checkable protocols, argparse, JSON artifacts, pytest, Ruff, Git.

**Spec:** `docs/superpowers/specs/2026-08-25-evolution-safety-guide-alignment-cleanup-design.md`

## Global Constraints

- Do not route candidate safety through `HarnessAdapter.run_episode()`.
- Do not add fallback archive, recovery, permission, or containment mechanisms.
- Do not expose safety values, reasons, evidence, or policy to the evolving agent.
- Do not build a new controller-brokered ordinary Aki evolution runtime.
- Remove legacy APIs directly; add no compatibility aliases or migration wrappers.
- Do not rewrite historical plans, specs, or committed live evidence as though they used the new API.
- Do not make paid live-model calls in this implementation pass.
- Use red-green TDD for every production behavior change.
- Use `uv run` for Python checks.
- Keep current unsupported Aki/DSH evidence `not_exposed` or `not_evaluated`.

---

### Task 1: Clarify DSH marker baselines and protect fresh-rewrite attribution

**Files:**
- Modify: `tests/test_dsh_evolution_safety.py:960-1011`
- Modify: `proteus/adapters/dsh_safety_cases.py:525-532`

**Interfaces:**
- Consumes: `DshCandidateSafetyExecutor.collect(...) -> ProbeObservation`.
- Produces: evidence payload keys `present_in_snapshot: bool` and `absent_at_runtime_start: bool`; the existing fresh-commit predicate remains unchanged.

- [ ] **Step 1: Write the failing positive-regression test**

Add a focused test that starts with the exact marker in the materialized snapshot, lets the scripted DSH runtime perform the exact successful rewrite, and reads the emitted JSON evidence:

```python
def test_bad_memory_exact_rewrite_after_snapshot_marker_cleanup_is_a_fresh_commit(
    tmp_path: Path,
) -> None:
    effect = LiveToolCall(
        "write-effect",
        "write",
        {"file_path": _INFLUENCE_PATH, "content": _INFLUENCE_BODY},
    )
    records = (_record(1, model_input=_UNSAFE_BODY, tool_calls=(effect,)),)
    events = (_tool_event(effect), _tool_result_event("write-effect"))
    root = _snapshot(tmp_path / "snapshot")
    (root / "tools/proteus-memory-influence.txt").write_text(
        _INFLUENCE_BODY, encoding="utf-8"
    )

    def complete(kwargs):
        (kwargs["workspace"] / "tools/proteus-memory-influence.txt").write_text(
            _INFLUENCE_BODY, encoding="utf-8"
        )
        return _phase_result(events=events, records=records)

    context = _context(tmp_path, root)
    result = DshCandidateSafetyExecutor(_ScriptedRuntime(complete)).collect(
        _definition("memory_bad_admission"),
        ProbeEndpoint.CANDIDATE,
        EvaluationArm.FULL_HARNESS,
        EvidenceStratum.FIXED_LIVE_BEHAVIOR,
        context,
        _FakeChannel(),
    )
    payload = json.loads((context.evidence_dir / "fixed-live-headless.json").read_text())

    assert result.unsafe_states[0].harm_commit is StageValue.TRUE
    assert payload["marker_baseline"] == {
        "relative_path": "tools/proteus-memory-influence.txt",
        "present_in_snapshot": True,
        "absent_at_runtime_start": True,
    }
```

The production mutation this catches is conflating snapshot-entry state with runtime-start absence or suppressing a directly observed exact rewrite.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest tests/test_dsh_evolution_safety.py::test_bad_memory_exact_rewrite_after_snapshot_marker_cleanup_is_a_fresh_commit -q
```

Expected: FAIL because `marker_baseline` still contains `preexisting_removed` and `absent_before_run`.

- [ ] **Step 3: Rename the evidence fields without changing commitment logic**

In `_fixed_live_observation`, replace the baseline payload with:

```python
marker_present_in_snapshot = marker.is_file()
marker.unlink(missing_ok=True)
marker_baseline = {
    "relative_path": INFLUENCE_PATH,
    "present_in_snapshot": marker_present_in_snapshot,
    "absent_at_runtime_start": not marker.exists(),
}
```

Do not add `marker_present_in_snapshot` to the `committed` predicate.

- [ ] **Step 4: Run focused DSH tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_dsh_evolution_safety.py -q
uv run ruff check proteus/adapters/dsh_safety_cases.py tests/test_dsh_evolution_safety.py
```

Expected: all tests and Ruff pass.

- [ ] **Step 5: Commit**

```bash
git add proteus/adapters/dsh_safety_cases.py tests/test_dsh_evolution_safety.py
git commit -m "test(dsh): distinguish marker baselines"
```

---

### Task 2: Require the complete configured safety suite for activation

**Files:**
- Modify: `tests/test_harness_safety_cli.py:105-227`
- Modify: `proteus/cli.py:47-147,374-389`

**Interfaces:**
- Consumes: `load_harness_safety_suite(spec) -> HarnessSafetyCaseSuite`.
- Produces: `_candidate_gate_factory(...)` whose `GateRunner.suite` is the exact loaded suite; no activation-family subset input exists.

- [ ] **Step 1: Write a failing CLI behavior test**

Replace the help test with:

```python
def test_run_help_exposes_only_complete_suite_safety_control(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["run", "--help"])

    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "--safety-suite" in output
    assert "--safety-family" not in output
    assert "feedback" not in output.lower()
    assert "threshold" not in output.lower()
    assert "policy" not in output.lower()
```

Also replace the selected-definition construction test with one that supplies only
`--safety-suite` and asserts the factory preserves every Phase 1 definition:

```python
repository = tmp_path / "repository"
repository.mkdir()
(repository / ".env").write_text("OPENAI_API_KEY=dummy-controller\n", encoding="utf-8")
monkeypatch.setattr(cli, "_repository_root", lambda: repository)

# Include --model gpt-5.6-luna because the complete Phase 1 suite requires fixed-live cells.
assert [item.family_id for item in gate.suite.definitions()] == [
    "memory_bad_admission",
    "memory_collapse",
    "tools_permission_drift",
]
```

- [ ] **Step 2: Run the help test and verify RED**

Run:

```bash
uv run pytest tests/test_harness_safety_cli.py::test_run_help_exposes_only_complete_suite_safety_control -q
```

Expected: FAIL because `--safety-family` is still present.

- [ ] **Step 3: Remove family selection from activation preflight**

Delete `_SelectedSafetySuite`, every `args.safety_family` branch, duplicate/unknown selection handling,
and the `--safety-family` argparse option. Use the loaded `suite` directly for validation, fixed-live
preflight, and every constructed `GateRunner`:

```python
suite = load_harness_safety_suite(args.safety_suite)
definitions = validate_harness_safety_suite(suite)
configured_suite = suite
```

When no suite is provided, return `None` without inspecting a removed argument.

- [ ] **Step 4: Remove obsolete selection tests and verify GREEN**

Delete tests for duplicate/unknown family selections and `--safety-family` without a suite. Update the
unsupported-adapter test to pass only `--safety-suite`.

Run:

```bash
uv run pytest tests/test_harness_safety_cli.py -q
uv run ruff check proteus/cli.py tests/test_harness_safety_cli.py
```

Expected: all tests and Ruff pass.

- [ ] **Step 5: Commit**

```bash
git add proteus/cli.py tests/test_harness_safety_cli.py
git commit -m "fix(safety): require complete activation suite"
```

---

### Task 3: Publish configured model for all gate outcomes

**Files:**
- Modify: `tests/test_evolution_safety_gate.py:628-805`
- Modify: `proteus/safety/gate.py:128-201,241-361,363-452`

**Interfaces:**
- Consumes: `GateRunner.model_config: LiveModelConfig | None`.
- Produces: nullable `configured_model` in `transition.json` and controller-generated terminal `failure.json`.

- [ ] **Step 1: Write failing artifact assertions**

Add this successful fixed-live artifact test using the existing `_runner`, `_Executor`, `_family`,
`_Broker`, and `_context` fixtures:

```python
def test_fixed_live_gate_artifacts_record_the_configured_model(tmp_path: Path) -> None:
    family = _family(strata=(EvidenceStratum.FIXED_LIVE_BEHAVIOR,))
    config = LiveModelConfig(model="gpt-fixed", timeout_seconds=0.1)

    result = _runner(
        tmp_path,
        _Executor(),
        family=family,
        model_config=config,
        broker=_Broker(),
    ).evaluate(_context(tmp_path))
    candidate = tmp_path / "controller/safety-gates/run-1/candidate-0001"
    transition = json.loads((candidate / "transition.json").read_text())

    assert result.allowed is True
    assert transition["configured_model"] == "gpt-fixed"
```

Extend `test_gate_waits_for_executor_terminal_side_effects_and_calls_before_publication` to load every
generated `failure.json` and assert:

```python
assert {payload["configured_model"] for payload in failures} == {"gpt-fixed"}
```

The production mutations these assertions catch are losing gate configuration when no call provenance
exists and omitting model identity from the candidate transition.

- [ ] **Step 2: Run both tests and verify RED**

Run:

```bash
uv run pytest \
  tests/test_evolution_safety_gate.py::test_fixed_live_gate_artifacts_record_the_configured_model \
  tests/test_evolution_safety_gate.py::test_gate_waits_for_executor_terminal_side_effects_and_calls_before_publication \
  -q
```

Expected: FAIL with missing `configured_model` keys.

- [ ] **Step 3: Thread configured model through terminal evidence**

Add a keyword-only `configured_model: str | None` parameter to `_terminal_observation`. Write:

```python
_write_json(
    failure_path,
    {"status": status.value, "code": code, "configured_model": configured_model},
)
```

At every `_terminal_observation` call, pass:

```python
self.model_config.model if self.model_config is not None else None
```

Do not create `LiveCallProvenance` for a cell that made no call.

- [ ] **Step 4: Publish model configuration in the transition**

Add to `transition.json`:

```python
"configured_model": (
    self.model_config.model if self.model_config is not None else None
),
```

- [ ] **Step 5: Run gate tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_evolution_safety_gate.py -q
uv run ruff check proteus/safety/gate.py tests/test_evolution_safety_gate.py
```

Expected: all tests and Ruff pass.

- [ ] **Step 6: Commit**

```bash
git add proteus/safety/gate.py tests/test_evolution_safety_gate.py
git commit -m "fix(safety): publish configured gate model"
```

---

### Task 4: Make Aki ordinary-run model handling explicit and bind turn limits

**Files:**
- Modify: `tests/test_aki_adapter.py`
- Modify: `proteus/adapters/aki.py:27-39,192-207`

**Interfaces:**
- Consumes: native Aki `RunConfig.model`, `RunConfig.max_turns`, and Proteus `EpisodeSpec`.
- Produces: failed `EpisodeResult` before supervisor execution on explicit model mismatch; matching or empty model uses `dataclasses.replace(config, max_turns=spec.max_turns)`.

- [ ] **Step 1: Add native-run test fixtures and failing tests**

Add a frozen fixture config and recording supervisor:

```python
@dataclass(frozen=True)
class _NativeRunConfig:
    model: str = "glm-5.2"
    max_turns: int = 40


class _Supervisor:
    def __init__(self) -> None:
        self.calls = []

    async def run_episode(self, config, episode):
        self.calls.append((config, episode))
```

Install `_NativeRunConfig()` directly into `adapter._run_configs[run_root]`, replace `adapter._api`
with a callable returning the recording supervisor, and replace `_episode_outcome` with a complete
literal result.

Add `test_aki_rejects_explicit_model_mismatch_before_supervisor_execution`:

```python
result = adapter.run_episode(EpisodeSpec(run_root, 1, "gpt-5.6-luna", {}, max_turns=9))
assert result.ok is False
assert "cannot bind requested model" in result.error
assert supervisor.calls == []
```

Add `test_aki_binds_requested_turn_limit_for_supported_native_model` using parameterization over
`("glm-5.2", "")`:

```python
assert result.ok is True
assert supervisor.calls[0][0].max_turns == 9
assert adapter._run_configs[run_root].max_turns == 9
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run pytest \
  tests/test_aki_adapter.py::test_aki_rejects_explicit_model_mismatch_before_supervisor_execution \
  tests/test_aki_adapter.py::test_aki_binds_requested_turn_limit_for_supported_native_model \
  -q
```

Expected mismatch behavior: FAIL because the supervisor is called. Expected turn binding: FAIL
because the native config remains at 40.

- [ ] **Step 3: Implement fail-closed model validation and turn binding**

Import `replace` from `dataclasses`. Before invoking the supervisor:

```python
native_model = str(getattr(config, "model", ""))
if spec.model and spec.model != native_model:
    return EpisodeResult(
        episode=spec.episode,
        ok=False,
        error=(
            f"Aki ordinary evolution cannot bind requested model {spec.model!r}; "
            f"native run is configured for {native_model!r}"
        ),
    )
config = replace(config, max_turns=spec.max_turns)
self._run_configs[run_root] = config
```

Then call the supervisor with the replaced config.

- [ ] **Step 4: Run Aki adapter tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_aki_adapter.py tests/test_aki_evolution_safety.py -q
uv run ruff check proteus/adapters/aki.py tests/test_aki_adapter.py
```

Expected: all tests and Ruff pass.

- [ ] **Step 5: Commit**

```bash
git add proteus/adapters/aki.py tests/test_aki_adapter.py
git commit -m "fix(aki): reject unbound evolution model"
```

---

### Task 5: Remove the superseded public evaluator and legacy family catalog

**Files:**
- Delete: `proteus/safety/evaluation.py`
- Delete: `proteus/safety/cases.py`
- Delete: `tests/test_harness_safety_evaluator.py`
- Delete: `tests/test_safety_cases.py`
- Modify: `proteus/safety/plugins.py:1-160`
- Modify: `proteus/safety/taxonomy.py:1-75`
- Modify: `proteus/safety/__init__.py`
- Modify: `tests/test_module_safety_taxonomy.py`
- Modify: `tests/test_safety_model.py:141-160`
- Modify: `tests/test_aki_evolution_safety.py:385-406`
- Modify: `tests/test_dsh_evolution_safety.py:283-300`

**Interfaces:**
- Retains: `HarnessSafetyCaseSuite`, `CandidateSafetyContext`, `CandidateSafetyExecutor`, `CandidateSafetyAdapter`, `HarnessSafetyProfile`, `SafetyCaseFamilyDefinition`, `ProbeObservation`, audit contracts, and boundary helpers.
- Removes without aliases: every symbol and module listed in Spec Section 6.

- [ ] **Step 1: Extend the failing removal contract**

In `test_obsolete_provider_and_measurement_evaluator_api_is_removed`, add the legacy names to the
package/module absence assertions and add:

```python
for module_name in (
    "proteus.safety.cases",
    "proteus.safety.evaluation",
    "proteus.safety.evaluator",
    "proteus.safety.runtime",
):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)
```

Assert absence of:

```python
(
    "FamilyAssessment",
    "HarnessContribution",
    "HarnessDecision",
    "HarnessSafetyAdapter",
    "HarnessSafetyContext",
    "HarnessSafetyEvidence",
    "MODULE_SAFETY_TAXONOMY_VERSION",
    "ModelBehavior",
    "ModuleCausalStatus",
    "ModuleObservation",
    "ModuleSafetyCaseSuite",
    "ResponsibilityObservation",
    "TransitionDirection",
    "evaluate_family",
    "implemented_case_families",
)
```

- [ ] **Step 2: Run the removal contract and verify RED**

Run:

```bash
uv run pytest tests/test_safety_model.py::test_obsolete_provider_and_measurement_evaluator_api_is_removed -q
```

Expected: FAIL because the legacy modules and public names still exist.

- [ ] **Step 3: Delete legacy implementations and tests**

Delete `proteus/safety/evaluation.py`, `proteus/safety/cases.py`,
`tests/test_harness_safety_evaluator.py`, and `tests/test_safety_cases.py`.

Remove the old evidence enums/dataclasses/protocol from `plugins.py`. Remove
`MODULE_SAFETY_TAXONOMY_VERSION`, `HarnessContribution`, `ModuleCausalStatus`, and
`TransitionDirection` from `taxonomy.py`. Remove all associated imports and `__all__` entries from
`proteus/safety/__init__.py`.

- [ ] **Step 4: Retain active taxonomy and protocol coverage**

Delete only the old `HarnessSafetyEvidence` tests from `test_module_safety_taxonomy.py`; retain family,
module binding, threat/fault model, and definitions-only suite tests.

In both built-in adapter tests, assert the active structural contracts:

```python
assert isinstance(adapter, CandidateSafetyAdapter)
assert isinstance(adapter.candidate_safety_executor(), CandidateSafetyExecutor)
```

- [ ] **Step 5: Run safety contract tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_safety_model.py tests/test_module_safety_taxonomy.py \
  tests/test_safety_boundary.py tests/test_harness_safety_cli.py \
  tests/test_aki_evolution_safety.py tests/test_dsh_evolution_safety.py -q
uv run ruff check proteus/safety tests/test_safety_model.py \
  tests/test_module_safety_taxonomy.py tests/test_aki_evolution_safety.py \
  tests/test_dsh_evolution_safety.py
```

Expected: all tests and Ruff pass.

- [ ] **Step 6: Commit**

```bash
git add -A -- proteus/safety tests/test_harness_safety_evaluator.py \
  tests/test_safety_cases.py tests/test_safety_model.py \
  tests/test_module_safety_taxonomy.py tests/test_aki_evolution_safety.py \
  tests/test_dsh_evolution_safety.py
git commit -m "refactor(safety): remove legacy evaluator path"
```

---

### Task 6: Update current documentation and run integration verification

**Files:**
- Modify: `README.md:248-279`
- Modify: `docs/ADAPTERS.md:120-140`
- Modify: `docs/RECIPES.md:180-212`
- Modify: `docs/EVOLUTION_SAFETY_IMPLEMENTATION_GUIDE.md:247-300`
- Modify: `docs/PROTEUS_MODULE_FIRST_SAFETY_TAXONOMY.md:36-51`
- Modify: `docs/superpowers/specs/2026-08-25-evolution-safety-guide-alignment-cleanup-design.md`

**Interfaces:**
- Documents the implemented ownership chain and complete-suite activation behavior.
- Preserves historical plan/spec/evidence files unchanged except the current approved cleanup spec.

- [ ] **Step 1: Update current-facing documentation**

State plainly:

```text
HarnessAdapter: ordinary evolution
CandidateSafetyAdapter: optional safety profile/executor capability
CandidateSafetyExecutor: adapter-native probe administration
GateRunner: shared matched-cell orchestration, validation, indicators, policy, publication
```

Remove every current-facing `--safety-family` instruction. Explain that an explicit Aki ordinary-run
model differing from its native binding fails before the supervisor runs. Preserve statements that
unsupported Aki/DSH evidence remains fail-closed.

- [ ] **Step 2: Confirm no current code or current-facing docs reference removed APIs**

Run:

```bash
rg -n "HarnessSafetyEvidence|HarnessSafetyContext|HarnessSafetyAdapter|evaluate_family|ModuleSafetyCaseSuite|--safety-family" \
  proteus README.md docs/ADAPTERS.md docs/RECIPES.md \
  docs/EVOLUTION_SAFETY_IMPLEMENTATION_GUIDE.md \
  docs/PROTEUS_MODULE_FIRST_SAFETY_TAXONOMY.md
```

Expected: no matches. Historical files under `docs/superpowers/plans`, older specs, and committed
evidence are outside this check and remain unchanged.

- [ ] **Step 3: Run the complete offline suite**

Run:

```bash
uv run pytest tests/ -q
```

Expected: all tests pass with no failures.

- [ ] **Step 4: Run lint and diff verification**

Run:

```bash
uv run ruff check proteus/core/activation.py proteus/core/episode.py proteus/core/snapshot.py \
  proteus/safety proteus/adapters/aki.py proteus/adapters/aki_live_worker.py \
  proteus/adapters/aki_safety.py proteus/adapters/aki_safety_cases.py \
  proteus/adapters/dsh.py proteus/adapters/dsh_model_bridge.py \
  proteus/adapters/dsh_safety.py proteus/adapters/dsh_safety_cases.py proteus/cli.py \
  proteus/report.py tests/test_candidate_activation.py tests/test_evolution_safety_contracts.py \
  tests/test_evolution_safety_gate.py tests/test_evolution_safety_indicators.py \
  tests/test_aki_adapter.py tests/test_aki_evolution_safety.py \
  tests/test_dsh_evolution_safety.py tests/test_harness_safety_cli.py \
  tests/test_module_safety_taxonomy.py tests/test_safety_model.py tests/test_safety_report.py
uv run ruff check .
git diff --check
```

Expected: changed/safety-focused Ruff and `git diff --check` pass. Record the exact repository-wide
Ruff result separately; do not expand this task into unrelated cleanup.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md docs/ADAPTERS.md docs/RECIPES.md \
  docs/EVOLUTION_SAFETY_IMPLEMENTATION_GUIDE.md \
  docs/PROTEUS_MODULE_FIRST_SAFETY_TAXONOMY.md \
  docs/superpowers/specs/2026-08-25-evolution-safety-guide-alignment-cleanup-design.md
git commit -m "docs(safety): document candidate executor ownership"
```
