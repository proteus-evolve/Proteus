# Additional method / harness pilots

These are **live synthetic mechanism checks**, distinct from the frozen research
studies and the Native/GEPA results in `RESULTS.md`. A configuration that passes
preflight is not an accepted live pilot. Each cell must retain its own outcome.

See [audited results](ADDITIONAL_RESULTS.md) for accepted cells, unchanged scores,
selected versus last candidates, and the preserved first-attempt failures.

| Cell | Optimized candidate | Real coding harness role | Minimum cycle |
| --- | --- | --- | --- |
| ACE | Typed playbook | Generator executes tasks with the current playbook rendered in its prompt | One training sample, reflection, curation, subsequent validation and private test |
| Combee-ACE | Typed playbook | Two independent generator workspaces | One two-sample batch, actual Combee aggregation, updated-playbook execution and private test |
| Meta-Harness | Python memory program | Workspace proposer reads disclosed history and writes candidate programs | One proposal iteration, interface checks, learning/prediction calls and final private evaluation |
| CORAL | Python task program | One supervised command worker invokes two fresh coding workspaces | First exact-file submission, released formal feedback, a second actor-produced program, formal evaluation and final selection/test |

ACE/Combee reflection and curation use managed DeepSeek chat. Meta-Harness's
candidate programs use managed DeepSeek chat through the original memory API.
These roles are explicitly different from the real Pi/DSH coding harness. Neither
ACE nor Meta-Harness is claimed to mutate the controlling harness's source code.

The first additional matrix uses Pi and requested model `deepseek-v4-flash`, with
the same pinned upstream image as the Native/GEPA pilot. DSH is a selectable
configuration, not accepted evidence until that exact cell runs. No old study is
rerun or retrospectively relabeled.

## First attempt and corrections

Run [40980909790](https://github.com/proteus-evolve/Proteus/actions/runs/40980909790)
at `3e8a0e6` failed overall. Its original outcomes and all costs remain retained.

- ACE's initial checker passed, but independent inspection found truncated JSON
  responses. Its first acceptance is withdrawn: changing a playbook is insufficient
  if the harness adapter loses the final answer before grading.
- Combee-ACE completed its batch, two aggregation proposals and reduction, but the
  checker looked for an incorrect evidence kind. Its generator replies also expose
  the same truncation defect. Completed-resume acceptance was not reached.
- Meta-Harness's real Pi proposer exhausted its tool budget without output files;
  the authoring image also lacked Python. Only baseline systems were evaluated.
  A controller that records a failed proposal is not a successful evolution pilot.
- CORAL produced two real changed programs and seven task rollouts; released
  validation progressed 0 → 0 → 1 and the selected program's private score was 1.
  Completed resume added no dispatch. This is the bounded fresh-session worker
  described above, not native continuation or multi-worker acceptance.

Corrections separate the complete final assistant response from shortened trace
previews (Pi/DSH/Codex), require parseable complete generator JSON, use the real
Combee evidence kind, and add Python to the authoring image. Meta-Harness retains
one proposal iteration but receives 64 tool-budget units and a 480-second phase
deadline. Its original 16-unit run is not reinterpreted. Exact derived image IDs
and installed Python versions are retained. The recovery selection runs only
ACE, Combee-ACE and Meta-Harness; it does not repeat the accepted CORAL cell.

The workflow now preserves actual failed step conclusions while allowing the next
independent selected cell to run. A failed overall workflow never becomes an
accepted all-method gate by combining successful steps from different runs.

## Execution

Build the production image using the pinned source steps in
`.github/workflows/native-source-integration.yml`. After setting the existing
`DEEPSEEK_API_KEY` through the environment, run one fresh cell:

```sh
python recipes/pilots/harness-roles/other_methods.py \
  --backend pi --image proteus-native-source:pi --method ace \
  --out /tmp/proteus-live-ace
```

Other method values are `combee-ace`, `meta`, and `coral`. Never reuse a directory
with an uncertain dispatch. The script pins the Docker image ID and repository
commit, saves recipe/lock, runs the method, verifies evidence, and checks a
completed resume for unchanged actor and worker dispatch receipts.

The opt-in Actions configuration is `live_role_pilot=true`,
`pilot_methods=other-methods`, `pilot_backends=pi`. Every cell has a separate step;
one failure does not prevent the other cells, and the aggregate job fails if any
selected cell fails. No credentials are stored in a recipe or report.

## Boundaries

- Combee-ACE uses real independent generator calls, with two workers. It is a
  batch/aggregation mechanism check, not a measured speedup or the eight historical
  fixed/dynamic/online/offline conditions.
- The CORAL pilot command bridge carries source and released feedback between
  fresh coding sessions. It does not claim persistent provider-session resumption,
  heartbeat intervention, multi-worker cooperation, or the paper's agent setup.
  Only the worker service owns its usage; unreported model calls/cost remain null.
- Meta-Harness candidates run in managed local processes on an ephemeral CI
  machine. This is not a candidate sandbox isolation claim.
- The checker requires real changed-candidate execution and feedback evidence,
  but does not require a stochastic optimizer to improve every score. Failed or
  incomplete attempts remain failures, with their traces and usage retained.
- Combee-GEPA, DSH/Codex combinations, and broader scientific experiments are not
  covered by these four cells.

The workspace image validator accepts both registry manifest digests and local
content-addressed image IDs. Parallel support is an explicit integration
capability: only fresh actor/workspace roles inherit it; Native continuity and
evolution roles do not. Built-in Pi/DSH use separate adapters, workspaces and
container names. Local concurrent-call tests and the live Combee cell exercise
these boundaries separately.

## DSH and Codex expansion (pending live acceptance)

The same four minimum cycles can be dispatched with `pilot_backends=dsh` or
`pilot_backends=codex` and `pilot_methods=other-methods`. DSH uses the same
DeepSeek configuration as Pi. Codex uses the existing production source image,
source revision `a7b86b6201d35200280e3d24ca5ebccd8122d6ca`, and explicitly requests
`gpt-5.4-mini` through the repository's existing OpenAI credential. The pinned
Codex model catalog lists this model as API-supported. Managed reflector,
curator and memory-system model roles still use `deepseek-v4-flash`; this is a
mechanism check, not a controlled comparison between harnesses or models.

Locally, supply `CODEX_API_KEY` (or `OPENAI_API_KEY`) and `DEEPSEEK_API_KEY`, and
add `--backend codex --harness-model gpt-5.4-mini` to the command above. The
CORAL bridge receives only the credential for its configured coding harness;
the parent service retains its own managed-model configuration.

Codex parallel capability is limited to fresh actor/workspace calls. Adapter
instances, auth copies, sessions, binary publications and receipts are isolated
per workspace. Offline concurrent tests cover these state boundaries; the live
Combee cycle must independently pass before claiming live support.

Codex builds its production Rust image once before all four cells. Previous
release validation took about 147 minutes for this build; that infrastructure
time is separate from method execution. After each successful completed-resume
check, `compact_codex.py` replaces repeated fixed-runtime binary copies with
size/SHA-256/source-hash manifests. Candidate programs, source snapshots,
native traces, input/outcome receipts and usage remain. These compact artifacts
are audit archives, not restartable working directories. Failed cells are not
compacted. Unknown costs and failed-attempt usage remain in evidence.

The first DSH/Codex expansion attempts (`40992089467` / `40992536635`) were
stopped after detecting an activation-copy defect: generic actors dereferenced
upstream source symlinks while copying the fixed harness to its active tree.
That changes the source hash and can trigger an unnecessary rebuild on every
actor invocation. A new source-identity regression failed before the fix and
passes when `copytree(..., symlinks=True)` preserves the pinned source. The
affected regression suite passed 75 tests with one environment skip. Codex was
still building its image and had not reached live method steps. DSH had entered
ACE; its partial provider usage remains unknown, not zero.

New attempts additionally check every native activation: Pi/DSH must report
the image's exact pristine source hash and `pristine` mode; Codex runtime
publications must match its image's source marker. Workflow live-step conditions
also refuse starting further cells after cancellation. Historical Pi results
retain their original source bindings and have not been silently revalidated
against this change.

The stopped DSH ACE cell also failed strict JSON verification. Its complete
artifact was not uploaded during force cancellation; the Actions log retains
the exception and attempt timestamps, while detailed usage remains unknown.
Inspection of earlier retained real DSH native logs then identified a separate
response-reader defect: multiple model `step`s share one dialogue `turn`.
Grouping visible text by turn prepended progress messages to the final JSON.
The updated regression uses that native event shape and failed before the fix.
DSH now returns the last complete visible assistant message; measurement traces
and private reasoning filtering are unchanged. Related regressions: 146 passed,
one environment skip. DSH attempt `40993899029` was stopped during image build
before inference to include this fix. Codex attempt `40993578545` continues on
`44380da`; the DSH-only reader correction does not change its adapter.

### DSH selective recovery

Run `40994189131` at `68980e4` completed with failures. ACE and CORAL passed
their full checks and completed resume, and their 13 evaluation rows were
independently regraded. Combee-ACE and Meta-Harness remain rejected:

- Combee's native logs contain `SANDBOX_UNAVAILABLE`: the requested nested
  `workspace-write` sandbox has no usable backend inside this CI Docker image.
  Repeated tool attempts exhausted two generator budgets (exit 137), leaving
  incomplete replies. All nine rows are retained; infrastructure failures are
  not accepted as zero performance scores.
- Meta's proposer prompt starts with YAML `---`. DSH's launcher and headless
  application each parse options, so both need a literal-argument separator.
  The original invocation was rejected before a proposer session started. Its
  six managed baseline calls and costs still belong to the failed attempt.

The DSH pilot now explicitly sets the inner `permission_mode=danger-full-access`
inside its existing Docker boundary; no privileged container flag is added.
The general adapter default stays `workspace-write`. Recipes can select this
DSH-only option, which is rejected for other backends. The adapter inserts two
`--` separators before its literal prompt. Receipt validation accepts both the
historical command shape and this delimited shape, while still rejecting resume
flags. The image's model-free cold-start probe now uses a dash-prefixed task.

The formatting dataset is version `live-playbook-mechanism-2`: it explicitly
requests one complete JSON object. The completeness checker also accepts a
single complete JSON Markdown fence, but rejects empty, truncated, multiple or
prose-mixed replies. This does not rehabilitate the earlier failed Combee cell.
Related local regressions passed 166 tests with one environment skip. A stale
Codex fixture was also corrected to mock final responses separately from trace
previews; its previous CI failure is retained.

To retry only these rejected cells, dispatch with `pilot_backends=dsh`,
`pilot_methods=other-methods`, and `pilot_cells=combee-ace,meta`. The workflow
validates the cell list and requires each selected cell to pass. Accepted ACE
and CORAL cells are not repeated. Results from distinct commits remain separately
bound evidence and do not form a same-SHA release gate.

The pinned Codex `exec` CLI also declares a positional prompt without accepting
hyphen-prefixed values. The adapter now inserts one `--` separator before its
literal prompt. A new boundary regression fails on the prior command and passes
after the change; related regressions passed 126 tests with one environment skip.
This is separate from DSH's two-parser fix. Run `40993578545` retains its original
`44380da` source; the newer delimiter change is not retroactively credited to it.

### Reusing a completed Codex producer's fixed runtime

For a rejected cell, `codex_reuse_run_id` can identify a completed explicit Codex
pilot workflow. The job downloads its artifact on the CI runner and accepts a
retained binary publication only when both binary hashes/sizes match the manifests
of successfully completed cells from that same producer, image and source pin.
It checks out the exact upstream source with its symlinks and requires the full
source hash to match the producer's pristine source witness. No Rust compilation
is repeated. A model-free, networkless probe tests the native CLI delimiter before
any new paid cell. Reuse validation has seven corruption/provenance regressions;
the affected local suite passed 48 tests.

The derived image has a new identity and is explicitly limited to fixed-runtime
actor/workspace roles; it rejects source changes and cannot stand in for a
source-evolution image. `reuse.json` binds its inputs to the original run. New
cells still have fresh run directories, recipe locks, traces and completed-resume
checks. Preserve the failed producer's outcomes and costs, and select only the
rejected method(s), for example `pilot_cells=meta`. Omitting the reuse input retains
the full production build path.

The first Codex Meta runtime recovery (`41009956547`) passed the native parser
probe and completed its real coding turn, but all three generated systems failed
loading. Full implementations were in an ignored `output/text_classification/`
mirror; the submitted `output/agents/` files imported themselves. The proposer's
mock checks tested the mirror instead of the candidate package. This failed
attempt is retained. Runtime instructions now explicitly override the reference
checkout paths and require a model-free check of the exact submitted package
using the existing `candidate.py` loader in a fresh scratch process. The loader
and acceptance criteria are unchanged; generated programs are not hand-edited.
