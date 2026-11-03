# Native evolution through shared candidate and evaluation services

This recipe is an offline engineering example. It executes a deterministic Python
subprocess harness, not a model, on two synthetic tasks. No API credential, Docker
image or optional reference package is needed. It is not a scientific experiment.

Use Python 3.10+ with this source revision installed. From the checkout:

```sh
PYTHONPATH=. python recipes/native/shared-evaluation/prepare.py --out /tmp/proteus-shared-native
python -m proteus.cli recipe lock /tmp/proteus-shared-native/recipe.json \
  --plugin proteus.examples.harness_roles:register --source proteus=. \
  --out /tmp/proteus-shared-native/lock.json
python -m proteus.cli recipe run /tmp/proteus-shared-native/recipe.json \
  --plugin proteus.examples.harness_roles:register \
  --lock /tmp/proteus-shared-native/lock.json --out /tmp/proteus-shared-native/run \
  --artifact-store /tmp/proteus-shared-native/artifact-store
python -m proteus.cli recipe resume /tmp/proteus-shared-native/recipe.json \
  --plugin proteus.examples.harness_roles:register \
  --lock /tmp/proteus-shared-native/lock.json --out /tmp/proteus-shared-native/run
python -m proteus.cli recipe export /tmp/proteus-shared-native/run \
  --out /tmp/proteus-shared-native/public-report
python -m proteus.cli recipe export /tmp/proteus-shared-native/run \
  --out /tmp/proteus-shared-native/owner-report --include-private
```

The script refuses to overwrite a nonempty output directory. Store outputs outside
the repository and keep the source fixed between lock, run and resume. Source or
configuration changes require a new lock and run.

Expected behavior:

| Candidate | Execution / gate | Observed task score |
| --- | --- | ---: |
| initial | program returns `2` for input `2` | 0 |
| episode-1 | intentionally invalid Python retained as pending; last-valid runtime remains active | no task dispatch |
| episode-2 | the last-valid program repairs the exact pending files; changed program returns `4` | 1 |
| episode-3 | repaired program now actually executes all phases | 1 |
| final hidden | same external exact-match evaluator; score appears only in owner export | 1 |

The managed ledger records four task evaluations and zero model calls. Completed
resume adds no dispatches. Inspect the actor's `WorkspaceInput` and native receipts
to distinguish the executing active artifact from the editable pending artifact.
The integration is registered once in `proteus.examples.harness_roles`; recipe role
bindings select target versus self-evolution instances.

The additional `proteus.native_evolution` profile uses the existing core phase
prompts, neutral disposition and last-valid activation. It does not translate the
legacy Native evaluator schedules, measurements or outer score selection. Existing
`proteus.native` and `proteus run` behavior remains available. See
[role contracts and limitations](../../../docs/V1_HARNESS_ROLES.md).
