# Additional method / harness pilots

These are **live synthetic mechanism checks**, distinct from the frozen research
studies and the Native/GEPA results in `RESULTS.md`. A configuration that passes
preflight is not an accepted live pilot. Each cell must retain its own outcome.

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
