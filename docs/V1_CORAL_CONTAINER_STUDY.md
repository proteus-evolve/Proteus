# Complete CORAL container study and circle-packing regrade

The acceptance study composes the existing `coral.single_island` method,
`proteus.worker.harbor` persistent research workers and the
[original circle-packing task](V1_CORAL_CIRCLE_PACKING.md). It exercises the same
controller and recipe interfaces intended for model-backed workers. The provider
here is a deterministic test script that publishes a known grid construction;
it is not a model optimizer or a paper-result reproduction.

Both conditions allow **three formal submissions**, plus the original seed
assessment and one private final confirmation: five evaluations total. One worker
uses three generations; two workers use four. Both dispatch two native
interventions. In the two-worker case, B reads A's published geometry, submits
that construction and publishes a skill citing both the note and formal attempt.
A waits for B's native continuation before its final submission. Readiness is
observed through the shared knowledge service rather than assumed from timing.

The seed's geometry is evaluated in Harbor. Every submitted candidate is frozen,
built and evaluated in its own trial; a separate verifier recomputes geometry.
Each worker generation receives a separate container with only its own persistent
workspace. The study verifies scoped workspace mounts, configured resources,
cleanup, native continuity, queue closure, final selection and no-repeat resume.
The two-worker controller also exits abruptly after the first scored result and
its committed checkpoint, while native workers remain active; resumption must
finish with the same five evaluations. The exit marker is bound to that journal
checkpoint in the retained evidence.
A public/private report pair and a descriptive single/multi-worker comparison
retain the original evidence and explicit budgets. Equal evaluation caps do not
establish equal monetary cost, and this fixture cannot demonstrate a collaboration
gain.

## Regrade an existing study

```sh
python -m proteus.benchmarks.coral_circle_packing.report /path/to/run \
  --include-private --out /path/to/packing-report.json
```

The report supports saved Harbor runs from the packing baseline/variant recipe
and complete CORAL recipe. It requires the pinned original initial program,
build protocol and verifier. It recomputes rewards from saved `packing.json`
without running candidate code, Docker or a model. Sealed trial receipts,
request/candidate/task identity, build receipts, verifier isolation and cleanup
must agree with the common formal grade records. Missing required bindings cause
a failure rather than silently dropping a task. Unscored infrastructure outcomes
with retained evidence remain unavailable; they are not converted to zero reward.

CORAL runs additionally include the [cooperation report](V1_CORAL_REPORT.md) and
verified Harbor worker-generation container records. The reporter omits private
scores and private evidence hashes unless `--include-private` is specified.
Output must be outside the source run and cannot overwrite an existing file.
Hashes establish consistency of the saved evidence, not independent authenticity.
Final confirmation is another execution of the same optimization problem, not a
held-out task.

## Acceptance commands

```sh
PROTEUS_HARBOR_PYTHON=/absolute/pinned/harbor/python \
PROTEUS_HARBOR_STUDY=/fresh/evidence/root \
python -m pytest tests/test_v1_coral_harbor_study.py -k live_harbor -q
```

The Harbor workflow runs this composition after the existing task/worker tests,
retains both complete runs and reports, and allows 45 minutes for the expanded
suite. A separate local process fixture checks the same research script's shared
geometry and native continuation protocol without Docker. Actual container
acceptance is required independently of those local fixture checks. Five-family
real-model results, active-wall-time measurement and release acceptance remain
separate requirements.
