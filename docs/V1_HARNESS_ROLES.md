# Harness integrations and configurable roles

This branch extends the proposed v1 API. It requires fresh configuration/source
locks; it does not reinterpret frozen studies or the acceptance of PR #106.

## What is shared

A `HarnessIntegration` declares one source-bound adapter factory, option schemas,
continuity and supported capabilities. `register_harness` generates ordinary
versioned actor plugins for its roles. The recipe independently binds each role to
an `ActorSpec`. Methods consume services and artifact/protocol capabilities; there
is no registry of method × harness pairs.

```text
Recipe.method ──────────────> optimization algorithm + checkpoints
Recipe.actors[role] ────────> configured harness integration instance
Method stage ──────────────> coding/evolution actor ──> immutable candidate
Candidate + pinned base ───> materialization ──> configured subject actor
Task + subject prediction ──> independent evaluator ──> permitted feedback
                                            └───────> private grader actor, if configured
```

A candidate is the optimized data: text components, playbooks, memory systems or
complete source files. A subject is the runner that executes the candidate for a
task. The configured subject actor supplies its harness runtime. A coding actor
can be another harness; its infrastructure is distinct from its output candidate.
Native self-evolution binds the evolution and target roles to the same integration.
Separate instances and working directories are created for every trial/dispatch.

## Integration and configuration

```python
from proteus.api.v1 import HarnessIntegration, register_harness

bindings = register_harness(registry, HarnessIntegration(
    name="my.harness",
    factory=make_adapter,  # (role, ActorSpec, typed_options) -> HarnessAdapter
    options={"actor": ActorOptions, "workspace": ActorOptions},
    implementation_hash=source_hash, source="my-source-pin",
    continuity="fresh",
    capabilities=("harness.custom_phases", "harness.workspace", "harness.candidate_tree"),
))
```

The example names are placeholders for an extension's real classes/factory and
source hash. Include all adapter/factory/helper dependencies in its source identity.
An integration must implement each advertised capability. Merely adding its name
to a recipe does not add tools, isolation, timeouts or state support.

Structured method roles consume the complete final visible assistant response.
Adapters that shorten measurement traces must implement
`read_response(root, episode)` separately from `read_trace`. Pi, DSH and Codex do
so; progress, reasoning and diagnostic trace entries are not a task answer. Trace
previews retain their existing limits, while response JSON must not be truncated.
An extension without this hook retains the legacy trace-text fallback and must
ensure that fallback actually satisfies its declared response contract.

`harness.parallel` is an explicit declaration that fresh independent calls may
overlap. Registration maps it to `actor.parallel` only for ordinary actor and
workspace roles, never Native continuity/evolution roles. Built-in Pi/DSH create
independent adapter instances, source copies and containers for these fresh calls.
Live method coverage and failures are recorded in the
[additional pilots](../recipes/pilots/harness-roles/OTHER_METHODS.md); a capability
declaration alone is not experimental acceptance.

Executable offline extension: [`harness_roles.py`](../proteus/examples/harness_roles.py).
It registers one Python subprocess harness for actor, workspace and evolution
roles. It is a deterministic mechanism fixture, not a production coding model.
The factory knows the role, but does not know the method.

## Candidate execution

Use `proteus.harness_subject`, bind `SubjectSpec.actor` to an ordinary harness actor,
and select `proteus.harness_environment` in the execution specification. The subject
uses that actor's model/settings/options; there is no second hidden model config.

- A `proteus.file_tree@1` candidate can be loaded directly.
- GEPA's `proteus.text_components@1` can overlay explicit `mutable_paths` on an
  immutable file-tree `base`. Both refs and codecs are imported/locked inputs.
  Declared paths are removed from the base before applying the overlay, so deleting
  a component cannot silently reactivate an old base file.
- Every evaluation trial copies files into a fresh root. It does not call `seed`
  over them. The output path must be absent from the input candidate; default:
  `notes/prediction.md`. Staged source adapters receive its writable
  `/workspace/candidate/` location explicitly; the active snapshot stays read-only.
  JSON output is an explicit option.
- Runtime evidence records original candidate hash, base hash, projection hash,
  effective harness hash and actual runtime/usage receipts. The subject ledger
  owns target-role usage once, independently of optimizer/grader calls.
- A source adapter's model-free validator runs before candidate execution.
  Invalid/missing prediction output and infrastructure failures remain separate
  outcomes. Missing usage/scores stay unknown, never zero.

Empty declared surface directories are recreated by adapter hooks; candidate source
and instructions are never reseeded or overwritten.

The materialization environment is explicitly trusted host + unrestricted network.
It enforces file bounds and identity, not process/network isolation. Static target
`ActorSpec.prompt_hash` is rejected: harness instructions belong in the pinned
candidate, so no configured prompt can be silently ignored. Containerized
adapters retain their own process boundary. A requested unsupported per-trial
transport deadline fails preflight; a container phase deadline is not substituted
for it. This bridge currently requires independent, serial target trials.

## Native evolution with external evaluation

`proteus.native_evolution` is an explicit additional Native profile. It reuses the
core four-phase prompt protocol and the same registered adapters, with ordinary
stages, immutable candidates, shared evaluation, budgets and the recovery journal.

Configure one `proteus.harness_episode` stage whose role uses
`proteus.evolution.<harness>`, and the subject role uses
`proteus.harness.<same-harness>`. Supply a complete initial file-tree artifact,
observed evaluation IDs, an explicit evidence policy and optional hidden final tests.
`NativeEvolutionOptions` names these IDs and the phase/turn controls. Built-in
evolution bindings use adapter options only; generic-call `max_turns` and `seed`
are rejected there. Episode turns belong to the method, and its seed to the recipe.

```text
last-valid candidate ─> frozen executing harness ─────────────────────┐
pending candidate ────> editable harness ─> four phases ─> output tree │
                                                        │           │
                                           model-free viability     │
                                     invalid ─> pending repair ──────┘
                                     valid ───> next last-valid
                                                 │
                                      shared external task evaluations
```

The episode result and output artifact are settled before any new task evaluation.
An interruption after settlement reuses them. Unknown actor dispatch outcomes keep
reservations and require reconciliation; resuming cannot regenerate a proposal.
Inline context is rendered only from the declared immutable evidence view, with
`max_inline_evidence_bytes` bounding its payload (default 65536). Zero disables
inline payloads while retaining the complete mounted view. Restricting evidence
kinds cannot be bypassed by injecting controller-visible scores into prompts.
A broken pending candidate is retained exactly for repair. For staged source
adapters all phases execute the last-valid source, including reflect. A viability
proof is bound to the exact output file components. A validator cannot modify the
candidate while declaring it valid.

This profile uses neutral disposition and last-valid activation, **without outer
score selection**. It evaluates the initial and every viable candidate on declared
observed pools; hidden tests run only after evolution on the last-valid candidate.
It does not implement the legacy evaluator CLI schedules, disposition manipulation,
checkpoint importing, surface/divergence measurements or rank/accept-reject
selection. The existing `proteus.native` recipe and `proteus run` retain those
semantics and are not silently translated. Completing the loop is not evidence
that every external evaluation succeeded: inspect each outcome and missing score.

A generic single-call bound does not bound a multi-phase episode. Evolution roles
therefore do not advertise a finite model-call bound unless the integration is
explicitly model-free. Unsupported finite role/stage/run caps fail before dispatch.
Known tokens/calls are still settled from native receipts; cost can remain unknown.

## External evaluators and private model grading

The same evaluator API receives task, recorded prediction and private reference
regardless of method/harness. Deterministic evaluators retain their existing path.
`proteus.model_judge` instead declares `evaluator.managed_actor`; its
`EvaluationSpec.actor` identifies an independent configured grading role.

The managed grading context permits one dispatch. It publishes private input and
outcome evidence bound to evaluation/rollout/task/candidate identity, uses a separate
workspace and ledger role, and preserves actual usage. Hidden grader requests,
reference answers and outcomes do not become public optimizer/subject actor calls,
values or feedback. A malformed answer is an infrastructure failure with a missing
score and retained usage. An unknown dispatch blocks redispatch. Restore and export
check evidence hashes and bindings without calling the model again.

In parallel evaluation, after-prediction grading requires `actor.parallel` and
its role budget limits the whole trial's concurrency. After-batch and hidden
grading run serially during finalization. Completed parallel trial recovery
verifies the same private input/outcome and native usage evidence before joining;
invalid evidence blocks recovery without a replacement dispatch. See
[parallel evaluation](V1_PARALLEL_EVALUATION.md) for the admission contract.

Plugins remain trusted controller code. A model grader that needs multiple calls
must expose a distinct managed protocol with explicit bounds; it cannot hide them
behind a deterministic declaration.

## Capability and evidence matrix

Query actual declarations with `harness_support_matrix(builtin_registry())`.

| Integration | Ordinary actor | Candidate subject | Coding workspace | Native episode role | Legacy Native |
| --- | --- | --- | --- | --- | --- |
| Minimal | yes, mock policy | no: policy does not consume candidate state | no | no | yes |
| LLM | yes | yes: files read by real HTTP adapter | no: fixed notes/tools actions | yes, non-staged files | yes |
| Pi / DSH / Codex | yes | declared source candidate bridge | yes | declared staged source role | yes |
| External | only declared roles | requires candidate-tree + generic actor | requires workspace role/capability | requires evolution + candidate-tree/custom phases | requires native binding |

Declarations do not mean every method accepts every representation. GEPA requires
text components; ACE a playbook; Meta-Harness its candidate-code protocol. Use an
explicit projection/runner for compatible representations. Incompatible choices
fail preflight instead of silently bypassing the method protocol.

Aki remains within the legacy Native CLI scope; it is not a sixth registered
common-role integration. The common candidate subject uses independent serial
trials, and the common Native evolution profile uses last-valid activation without
outer score selection. These boundaries also apply when interpreting the pilots.

Evidence currently distinguishes:

1. The actual LLM adapter with a local HTTP provider is optimized by the true pinned
   GEPA loop and the Native loop. Both load changed state and use exact-match task
   grading. The provider is deterministic; these are not model-quality results.
   The same GEPA reflection role also switches between the actual LLM harness and
   the separate Python subprocess implementation through configuration alone.
2. The subprocess integration executes source from the active artifact, rejects a
   syntax-broken candidate, repairs the exact pending files, then activates changed
   source. The same integration serves as a coding actor; a second registration
   replaces that role through configuration. These are fixture programs.
3. Hard-exit tests distinguish settled versus unknown Native/grader work, and
   completed resume adds no dispatches. Corrupted subject/grader receipts block
   restore/export. Workspace input/history isolation is checked separately.
4. Real Pi/DSH Native and GEPA cells have separate
   [source-bound live evidence](../recipes/pilots/harness-roles/RESULTS.md).
   Additional Pi ACE/Combee generator, Meta coding-proposer and bounded CORAL worker
   cells have [audited results](../recipes/pilots/harness-roles/ADDITIONAL_RESULTS.md).
   These test specified roles and cycles; ACE/Combee/Meta did not outperform their
   baselines in that Pi pilot. DSH and Codex now each have independently accepted
   ACE, Combee-ACE, Meta proposer and bounded CORAL worker cycles in the
   [eight-cell report](../recipes/pilots/harness-roles/DSH_CODEX_RESULTS.md).
   Candidates execute through their specified roles; scores and selected versus
   last candidates remain separate. Codex Native/GEPA and Combee-GEPA are not
   covered by this expansion. Different recorded source commits
   do not form a single-SHA release gate. Existing PR #106 source gates alone are
   not evidence for new method/role combinations.

Together these reports cover 16 bounded live cells. See the
[release evidence map](V1_RELEASE_EVIDENCE_MAP.md) for their I03 acceptance and the
remaining benchmark/release requirements; a successful cycle is not full research
acceptance or a promise for every role combination.

## Reproduce mechanism checks

Install the repository dev extras and the exact optional GEPA source named in
[`reference.py`](../proteus/methods/gepa/reference.py). From the checkout:

```sh
python -m pytest tests/test_v1_harness_integrations.py tests/test_v1_harness_candidates.py \
  tests/test_v1_harness_methods.py tests/test_v1_model_evaluators.py \
  tests/test_v1_workspace.py tests/test_v1_harness_accounting.py -q
```

For an end-to-end CLI recipe with exact expected outputs, see the
[offline Native/shared-evaluation example](../recipes/native/shared-evaluation/README.md).

Only loopback HTTP servers, dummy credentials and deterministic subprocess programs
are used. The tests prepare their own versioned datasets, immutable artifacts,
recipes and source/dependency locks. Keep the test source fixed until recovery
subprocesses finish. Read the raw outcomes and fixture limitations with the results;
passing them does not establish paper replication or scientific benefits.


Optional new fields are omitted when unused: `EvaluationSpec.actor`, `Grade.runtime`
and ordinary workspace evolution controls. This preserves pre-role configuration,
request and grading record hashes. Actual pre-change comparison and workspace runs
from `325ab5e` produce identical reports under the new reader. This is read
compatibility, not permission to resume an old run with changed implementations.
