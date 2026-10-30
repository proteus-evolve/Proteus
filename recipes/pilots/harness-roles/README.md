# Live harness role pilot

This pilot tests the new `harness_subject` / `native_evolution` bridges with the
actual pinned Pi and DeepSeek Harness source images and a real DeepSeek model.
It is a mechanism check on synthetic formatting tasks, not a benchmark result or
a matched reproduction of any paper. No tool action or model reply is mocked.

See [`RESULTS.md`](RESULTS.md) for source-bound outcomes, original failures and
acceptance boundaries; [`results.json`](results.json) contains compact evidence.

Additional Pi pilots for ACE, Combee-ACE, Meta-Harness and CORAL are described in
[`OTHER_METHODS.md`](OTHER_METHODS.md), with audited outcomes in
[`ADDITIONAL_RESULTS.md`](ADDITIONAL_RESULTS.md). Their role and source boundaries
are distinct from the four Native/GEPA cells below.
The subsequent eight DSH/Codex cells are independently accepted in
[`DSH_CODEX_RESULTS.md`](DSH_CODEX_RESULTS.md), with machine-readable identities
and audited rows in [`dsh-codex-results.json`](dsh-codex-results.json).

## Four cells

| Target harness | Method | Optimized artifact | Acceptance |
| --- | --- | --- | --- |
| Pi | Native, one episode | Full harness tree | Live agent changes its TypeScript entry and policy; the next task executes the rebuilt CLI and updated policy. |
| DSH | Native, one episode | Full harness tree | Same recipe structure, different configured integration. |
| Pi | GEPA, one mutation | `notes/policy.md` over a pinned harness base | Pinned official GEPA reflects on actual feedback; the candidate policy is executed by real Pi tools. |
| DSH | GEPA, one mutation | `notes/policy.md` over a pinned harness base | Same method, evaluator and projection with DSH as the configured subject harness. |

Native uses the same integration for the evolution actor and subject. Its explicit
engineering goal asks for an uppercase policy and an observable CLI marker. GEPA
uses a managed DeepSeek chat reflection actor, as appropriate to its text-mutation
protocol; the task subject is the full Pi/DSH harness. This pilot does not claim
that GEPA changes TypeScript source or that all other method/harness pairs work.
The DSH seed explicitly copies its eight pinned documentation/fixture symlink
aliases to ordinary files/directories, preserving their current contents. This
projection is recorded in `seed-projection.json`; alias identity is not preserved.
Unknown, broken or source-escaping links fail. The core artifact codec continues
to reject symlinks. Neither backend's executable source is filtered out.

The initial rule lowercases mixed-case text. Train, validation (GEPA only) and
private final cases use different words. The same external exact-match evaluator
grades the real output file. The candidate carries no prediction file. Private
test references never enter the optimization feedback. These tiny cases establish
wiring and activation only; their scores are not research performance measures.

## Reproduce

Use Linux with Docker host networking. Build the exact images from
`.github/workflows/native-source-integration.yml`: Pi revision
`914cf1472e715297caa30db4b9535d534a9eb718`, DSH revision
`99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`. The image label must record that revision.
Install Proteus with the `dsh` extra and GEPA at
`8b0ce6cd99a234f6b74daf37558a2ac0ce18f975`.

Set `DEEPSEEK_API_KEY` outside the recipe. The requested model is
`deepseek-v4-flash`; source identities, native receipt model information and usage
are recorded. For each backend (`pi`, `dsh`) and method (`native`, `gepa`):

```sh
python recipes/pilots/harness-roles/run.py \
  --backend pi --image proteus-native-source:pi --method native \
  --out /tmp/proteus-pilot-pi-native
```

Or explicitly dispatch the existing **Native source integration** workflow on the
pilot branch with `live_role_pilot=true` (optionally `pilot_methods=gepa` or `native`).
`pilot_methods=pi-gepa-dsh-both` runs the three cells still needing acceptance after
the first pilot, without repeating the already accepted Pi Native episode.
Ordinary PR/tag/manual gate runs do not
call a paid provider unless this input is set. The GitHub runner first verifies the
existing source gates, then runs both new-method cells independently. Each cell has
a 25-minute wall timeout. Native has one four-phase episode with 32 configured
turns; target trials allow 10 turns and 240 seconds each. GEPA has a three-metric-call
threshold and a six-evaluation hard ceiling. With one validation item and one-item
parent/child minibatches, this stops after the first mutation even if it is rejected.
Native adapters do not advertise a
hard provider-request or dollar bound; turn controls are not mislabeled as such.

Use a fresh output directory. Failed or uncertain dispatches are preserved and are
not automatically retried. A successful cell is immediately resumed once to check
that it does not add dispatch receipts. An invalid evolution or infrastructure
failure is a failure, not a score of zero.

## Evidence and limits

Each cell saves its recipe, lock, source/image identity, immutable candidates,
native execution/activation receipts, actual predictions, separate evaluator
records, report, result and final `evidence.json`. The workflow retains failed
cells as well. Credential-store files are excluded and credential bytes block the
entire artifact upload. Do not upload API keys or copy auth into candidate trees.

Passing requires changed candidate execution, accurate external grading of each
prediction, native usage receipts and zero new dispatches on completed resume.
Native additionally requires the explicitly requested lowercase-to-uppercase change,
a source marker, a successful viability build and distinct compiled CLI hashes in
task execution. GEPA requires exactly one reflection cycle; a rejected candidate
is valid mechanism evidence and its score is reported without calling it an improvement.
Costs absent from native reports remain unknown; token/call measurements are
reported separately. This does not validate ACE, Meta-Harness, CORAL or Codex through
these bridges, nor establish optimization quality, efficiency or release readiness.
