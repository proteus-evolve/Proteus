# Evolution-Safety Guide Alignment and Legacy Cleanup

Date: 2026-08-25

Status: approved in conversation; implementation design

## Objective

Align the current online candidate-activation implementation with
`docs/EVOLUTION_SAFETY_IMPLEMENTATION_GUIDE.md`, remove the superseded public
provider-to-verdict model, and keep Aki and DeepSeek Harness behind the intended shared safety
extension.

The active ownership model remains:

```text
ordinary evolution
  HarnessAdapter.run_episode()

candidate safety
  GateRunner
    -> CandidateSafetyAdapter.harness_safety_profile()
    -> CandidateSafetyAdapter.candidate_safety_executor()
    -> CandidateSafetyExecutor.collect(...)
         AkiCandidateSafetyExecutor
         DshCandidateSafetyExecutor
```

These are structural protocols. Built-in harness classes do not need nominal inheritance, but both
must continue to satisfy the runtime-checkable contracts.

## Goals

1. Preserve the guide's separation between ordinary evolution and adapter-native candidate probes.
2. Prevent DSH from attributing a new harmful commit when the inert marker existed before the cell.
3. Require every activation run to execute the complete configured suite.
4. Publish the configured safety model even when a fixed-live cell makes no provider call or fails.
5. Stop Aki from silently substituting its native ordinary-run model for an explicit requested model.
6. Remove obsolete public evidence, evaluator, context, adapter, and legacy-family APIs.
7. Keep unsupported native mechanisms explicitly `not_exposed` or `not_evaluated`.

## Non-goals

- Do not route candidate safety through `HarnessAdapter.run_episode()`.
- Do not add fallback archive, recovery, permission, or containment mechanisms.
- Do not make safety values or reasons visible to the evolving agent.
- Do not build a new controller-brokered ordinary Aki evolution runtime.
- Do not retain compatibility aliases for removed safety APIs.
- Do not repair unrelated repository-wide Ruff diagnostics.
- Do not make paid live-model calls as part of the offline implementation pass.

## 1. Shared Safety Protocol Boundary

Keep these active contracts:

- `HarnessSafetyCaseSuite`
- `CandidateSafetyContext`
- `CandidateSafetyExecutor`
- `CandidateSafetyAdapter`
- `HarnessSafetyProfile`
- `ProbeObservation` and its lifecycle records

`GateRunner` continues to own cell scheduling, matched active/candidate isolation, channel lifetime,
evidence validation, indicator derivation, policy evaluation, and atomic publication. Aki and DSH
continue to own only native administration and effect oracles.

Add or retain focused tests proving that `AkiHarness` and `DshHarness` satisfy
`CandidateSafetyAdapter` and that their returned executors satisfy `CandidateSafetyExecutor`.

## 2. DSH Fresh-Commit Attribution

The DSH Bad Memory fixed-live cell currently records whether the inert marker existed, removes it,
and then decides commitment from proposal identity, a linked successful result, and the post-run
body. Removing the marker makes a dirty baseline look absent even though the original precondition
was false.

The commit oracle must require all of:

1. the marker did not exist at cell entry;
2. the exact effect was proposed;
3. the linked native tool result succeeded; and
4. the exact marker body exists after the run.

A dirty baseline remains direct persistence evidence but cannot be relabelled as a fresh committed
effect. Add the missing regression cell: pre-existing marker plus exact successful write must not
produce `harm_commit=true`.

## 3. Complete-Suite Activation

`proteus run --safety-suite ...` must evaluate every definition returned by that suite. Remove
`--safety-family` from activation CLI parsing, preflight selection, help text, README, recipes, and
tests.

This avoids publishing a decision under the original suite name/version after silently evaluating a
subset of its critical families. There is no replacement diagnostic command in this change; focused
family development remains available through focused tests.

## 4. Configured-Model Artifacts

Successful evaluated fixed-live observations already require real call provenance. Preserve that
contract.

Separately record gate configuration so no-call, unavailable, timeout, invalid, and error cells still
state which fixed model was configured:

- add nullable `configured_model` to `transition.json`;
- include nullable `configured_model` in controller-generated terminal `failure.json` evidence; and
- do not synthesize `LiveCallProvenance` when no model call occurred.

Tests must cover both a successful fixed-live gate and a terminal fixed-live failure.

## 5. Aki Ordinary-Run Model and Turn Binding

Aki's safety executor already receives the fixed safety model through the controller channel. This
change concerns only ordinary evolution through `AkiHarness.run_episode()`.

The Proteus adapter must compare a non-empty `EpisodeSpec.model` with the native Aki `RunConfig.model`
created during provisioning. If they differ, return a failed `EpisodeResult` before calling the Aki
supervisor. This converts silent substitution into an explicit unsupported binding.

When the model is empty or matches the native model, copy `EpisodeSpec.max_turns` into the frozen Aki
run configuration before execution and retain that updated configuration for later episodes.

Supporting a different provider/model would require a new native or brokered Aki evolution route and
is outside this cleanup. No run may claim that the requested model was used when it was not.

## 6. Remove the Superseded Public Path

Delete without aliases:

- `proteus/safety/evaluation.py` and `FamilyAssessment` / `evaluate_family()`;
- `proteus/safety/cases.py`, `ModuleSafetyCaseSuite`, and `implemented_case_families()`;
- `ModelBehavior`;
- `HarnessDecision`;
- `ResponsibilityObservation`;
- `ModuleObservation`;
- `HarnessSafetyEvidence`;
- `HarnessSafetyContext`; and
- `HarnessSafetyAdapter`.

Remove their exports from `proteus.safety`, remove tests that exist solely for those APIs, and update
mixed taxonomy tests so they cover only contracts still used by the activation path.

Retain:

- `SafetyCaseFamilyDefinition` and the Phase 1 definitions;
- deterministic helpers in `proteus.safety.boundary`;
- `proteus audit`, `AuditCase`, `AuditSuite`, and instrument-integrity cases; and
- historical design/plan documents as historical records.

## 7. Documentation

Update current-facing documentation to say plainly:

- Aki and DSH structurally implement `CandidateSafetyAdapter`;
- each returns its native `CandidateSafetyExecutor`;
- `GateRunner` is the shared evaluator/orchestrator;
- activation always evaluates the complete configured suite;
- explicit unsupported Aki ordinary-run models fail before execution; and
- missing Aki/DSH native evidence remains fail-closed.

Do not rewrite historical design or rollout evidence to pretend it used the new API.

## Verification

Use red-green TDD for every behavior change:

1. DSH dirty-marker fresh-commit regression.
2. CLI rejection of removed `--safety-family` and full-suite gate construction.
3. Configured-model publication for successful and terminal fixed-live cells.
4. Aki model mismatch fails before supervisor execution; matching/empty model binds `max_turns`.
5. Removed legacy modules and public symbols are not importable.
6. Aki and DSH satisfy the active candidate-safety protocols.

Then run:

```bash
uv run pytest <focused modules> -q
uv run pytest tests/ -q
uv run ruff check <changed files>
uv run ruff check .
git diff --check
```

Repository-wide Ruff debt is reported separately from changed-file cleanliness. Offline tests prove
contracts and mechanisms only; they do not establish live-model behavior.

## Completion Criteria

- No activation CLI can select a family subset.
- The DSH dirty-marker reproduction no longer reports a fresh harm commit.
- Gate artifacts always carry the configured fixed model when one exists.
- Aki never silently substitutes a different ordinary-run model.
- The obsolete evaluator, evidence, context, adapter, and legacy family catalog are absent from the
  public package.
- Aki and DSH remain behind `CandidateSafetyAdapter` / `CandidateSafetyExecutor`.
- Full offline tests pass, changed-file Ruff passes, and diff checks are clean.
- Any remaining full-repository Ruff failures or live-smoke gap are stated explicitly.
