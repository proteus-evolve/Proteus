# Harness integration, role binding and external evaluation

Status: registration, candidate execution, managed grading and the additional
Native evolution profile are implemented. See [current contracts and evidence
boundaries](../V1_HARNESS_ROLES.md). The audit below describes the original baseline. Baseline: `325ab5e` / PR #106; its engineering
acceptance remains separate from all changes on this branch.

## Product contract

A harness integrates once. A recipe binds independently configured instances to
roles; a method implements its algorithm against capabilities. The same integration
may supply an evolving subject, a coding actor, or another supported execution role.
External task evaluation is independent of harness branding and method selection.
An unsupported role must fail validation before dispatch. An integration is not
required to support a capability its runtime cannot implement.

Candidates retain method-specific representations: GEPA text components, ACE
playbooks, candidate-code memory systems, or complete harness files. A reusable,
explicit materialization bridge connects a representation to an integration's
editable state. Running a coding agent that edits a separate system is not evidence
that the coding agent's own candidate state was activated.

## Baseline audit

| Area | Reuse | Actual gap |
| --- | --- | --- |
| Recipe | `ActorSpec`, `SubjectSpec`, `ExecutionSpec`, `EvaluationSpec`; plugin locks | Harness integration is expanded into a fixed built-in name loop rather than one extension registration flow. |
| Harness actor | `HarnessActor`, normalized traces, Native receipts and usage; workspace service | Factory/options/workspace validation is brand-specific. Generic calls always seed a fresh harness and cannot load the optimized candidate. |
| Candidate | Immutable artifact store, codecs, exact environment materialization, lineage, source activation | No reusable subject bridge loads a candidate into an arbitrary adapted harness and records the effective executable state. |
| Methods | Independent GEPA/ACE/Meta-Harness/Combee/CORAL algorithms | Their artifact/protocol requirements must stay explicit. These are not a reason to restrict harness brands. |
| Evaluation | Prediction-before-feedback, separate private grades, task/split identities, usage and recovery | Model graders have no independent managed-role interface; only deterministic or declared integrated-backend graders are accepted. |
| Native | Four-phase protocol, staged activation, recovery and legacy evaluator schedules | `validate_native` rejects the common Subject/Execution/Evaluation configuration. A common-contract bridge must preserve transaction and feedback semantics. |
| Recovery | Managed dispatch ledger, immutable results, replay journal and run locks | New roles and materialized candidate state must use these services; unknown outcomes cannot be handled by starting another harness. |

## Implementation sequence

1. Add a source-bound harness integration declaration and one registration helper.
   Generate supported actor/native/workspace/subject bindings from the declaration;
   keep existing built-in plugin names as compatibility aliases. Runtime construction
   belongs to the integration, not to each method or a growing brand switch.
2. Add candidate export/materialization/validation/activation and subject execution
   bridges. Pin base state, candidate representation, role instance, environment and
   effective loaded files. Do not reseed over the candidate. Separate immutable
   active execution state from editable output, and isolate every role/trial root.
3. Route Native configurations with external evaluation through journaled candidate
   operations and the existing evaluation service. Reuse its phase protocol while
   retaining the original legacy CLI/input form unchanged. The explicit
   `proteus.native_evolution` profile does not silently translate legacy evaluator
   schedules, measurements or score selection. Persist
   candidate-producing actor results before evaluation so an interruption cannot
   force an unknown operation to run again.
4. Expose external graders through the common evaluation contract, including an
   explicitly managed, private model-grading role. Reference answers and hidden
   grader prompts/results must not enter optimizer/subject evidence. Retain measured
   usage and unknowns on failures; never label model grading deterministic/free.
5. Add reproducible examples, extension instructions and a capability matrix. Keep
   the scientific profiles and historical recipes unchanged. Verify installed
   distributions and supported real adapter paths separately from fixture tests.

The registration helper reuses versioned plugin identities, source/implementation
hashes, constructor validation and dependency locks. It must not create a second
unversioned global registry or make arbitrary imports the recovery identity.

## Acceptance cases

| Case | Required evidence |
| --- | --- |
| One integration, two evolving methods | In two applicable methods, a changed candidate is materialized into that harness's editable state; the next actual execution observes the change and the external evaluator records its candidate identity. A canned candidate comparison alone is insufficient. |
| Same integration as coding actor | A separate recipe uses the same adaptation implementation to edit another system. The coding role's runtime and its output candidate remain distinguishable. |
| Role replacement | One method switches at least one role between two adapted harnesses using configuration only, with no method-source edit. |
| Shared external evaluator | The same evaluator plugin runs across harness/method combinations with the same task/reference protocol, grade schema and feedback rules. Model-grader usage is attributable and private. |
| Isolation and replay | Save/reload/completed resume preserves identities and adds no dispatches; concurrent roles/candidates do not share mutable trees. Hard-exit and unknown-outcome tests retain reservations and block redispatch. |
| Capability rejection | Missing candidate/workspace/session/timeout/usage capabilities fail before dispatch with the missing capability identified. |
| Real adapter boundary | Local-provider or real-container evidence verifies actual adapter inputs, loaded candidate state and activation. Fixtures are labeled explicitly and are not quality studies. |

## Scope and publication

Use offline/local-provider and bounded engineering checks. Do not restart AWS or
historical paid studies, change frozen results or expand provider budgets. PR #106
stays independent and its four successful workflows do not validate this branch.
The completed release-followup automation remains paused. No merge, version/tag,
GitHub Release or PyPI publication is authorized by this architecture work.
