# Live harness role pilot

This pilot tests the new `harness_subject` / `native_evolution` bridges with the
actual pinned Pi and DeepSeek Harness source images and a real DeepSeek model.
It is a mechanism check on synthetic formatting tasks, not a benchmark result or
a matched reproduction of any paper. No tool action or model reply is mocked.

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
pilot branch with `live_role_pilot=true`. Ordinary PR/tag/manual gate runs do not
call a paid provider unless this input is set. The GitHub runner first verifies the
existing source gates, then runs both new-method cells independently. Each cell has
a 25-minute wall timeout. Native has one four-phase episode with 32 configured
turns; target trials allow 10 turns and 240 seconds each. GEPA has a four-metric-call
threshold and a seven-evaluation hard ceiling. Native adapters do not advertise a
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

Passing requires changed candidate execution, lowercase-to-uppercase observed
behavior, native usage receipts and zero new dispatches on completed resume.
Native additionally requires a successful viability build and distinct compiled
CLI hashes in task execution. GEPA requires exactly one reflection cycle.
Costs absent from native reports remain unknown; token/call measurements are
reported separately. This does not validate ACE, Meta-Harness, CORAL or Codex through
these bridges, nor establish optimization quality, efficiency or release readiness.
