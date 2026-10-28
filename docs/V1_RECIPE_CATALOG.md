# Recipe discovery and search-policy experiments

For original method papers, official code and exact source pins, see
[method references](METHOD_REFERENCES.md) and [references.bib](../references.bib).

The catalog lists eight offline mechanism examples and five original-domain study
entry points. Discovery imports no optional optimizer or container backend and makes
no model calls. `recipe list` continues to list registered plugins and schemas.

```sh
proteus recipe catalog
proteus recipe catalog --family coral
proteus recipe catalog --kind benchmark
proteus recipe catalog gepa-aime
proteus recipe example ace --out /tmp/ace-example
```

Each entry states whether it is an offline fixture or a benchmark preparation
entry point, its requirements, plugin hooks, preparation command and documentation.
The catalog's `model_results_pending` status applies to its generic preparation
templates, not the separately frozen October studies. The
[experiment recipe index](../recipes/README.md) supplies all 15 accepted profiles,
their exact configurations, launchers, source overlays and recorded results.
Listing a runnable method is not evidence of reproduced model quality. Commands require explicit
data, actor settings or worker/runtime configuration. `recipe example` accepts only
offline examples and does not supply a benchmark's missing inputs.

Preparing an example writes `recipe.json`, any required task/artifact data, and
`example.json` with the resolved configuration hash and check/lock/run/resume commands.
The printed commands are argument arrays. Replace `<PROTEUS_GIT_ROOT>` with the
repository matching your installation before locking. Place outputs outside that
source tree or in an ignored directory. No lock is invented or model run started
by preparation. Optional GEPA examples require the pinned dependency documented in
[V1_GEPA_METHOD.md](V1_GEPA_METHOD.md).

## Replace a final selector without changing the engine

```sh
proteus recipe example search-policy --out /tmp/policy-study
```

The recipe evaluates three fixed candidates on the same two validation tasks:

| Candidate | Task 1 | Task 2 | Mean | Minimum |
| --- | ---: | ---: | ---: | ---: |
| initial | 0.9 | 0.1 | 0.50 | 0.1 |
| balanced | 0.6 | 0.6 | 0.60 | 0.6 |
| spiky | 1.0 | 0.3 | 0.65 | 0.3 |

The default `proteus.scalar_best` final policy chooses `spiky`. Copy the generated
recipe to another JSON file and change only `search.final.plugin.name` to
`example.maximin`. That policy chooses `balanced` by its minimum task score. Lock
each configuration separately and use distinct run directories, keeping the same
artifact store and task file. Both policies are registered by the same hook:

```sh
proteus recipe check /tmp/policy-study/recipe.json \
  --plugin proteus.examples.search_policy:register
proteus recipe lock /tmp/policy-study/recipe.json \
  --plugin proteus.examples.search_policy:register \
  --source proteus=/absolute/path/to/Proteus --out /tmp/policy-mean.lock.json
proteus recipe run /tmp/policy-study/recipe.json \
  --plugin proteus.examples.search_policy:register \
  --lock /tmp/policy-mean.lock.json --artifact-store /tmp/policy-study/artifact-store \
  --out /tmp/policy-mean-run
```

The method evaluates the chosen candidate on one private test task after selection.
The public test receipt has no score. Each condition uses seven formal evaluations:
six validation trials and one private test. The example's two final test scores
are deliberately different; neither is visible to the selection policy.

This is a controlled API demonstration, not a model benchmark or evidence that
maximin is generally better. The repository checks verify that changing only the
policy preserves the candidate artifacts and validation scores while changing the
chosen candidate. They also terminate the controller at evaluation, selection and
confirmation checkpoints, then verify recovery without duplicate evaluation charges.

## Implement a different policy

The complete example is in `proteus.examples.search_policy`. Its `Maximin` policy
implements `select_final(view, rng)` and returns a `PolicyResult` with chosen IDs
and reasons for every eligible candidate. It is registered as kind `policy` with
the `search.final` capability. A new policy can use the same interface, option
schema and implementation hash in an external plugin; load its registration hook
and name it in `search.final`.

Policies receive an immutable view of comparable, visible scores. Use the supplied
RNG for random decisions; external calls belong in method/stage services. The
framework validates eligible IDs, capabilities and recorded decisions. See
[V1_SEARCH.md](V1_SEARCH.md) for parent sampling and archive retention interfaces,
and [V1_PLUGIN_SCAFFOLDS.md](V1_PLUGIN_SCAFFOLDS.md) for typed self-evaluator projects.

Catalog discovery and this tutorial accompany recipe check/lock/run/resume,
[saved-run export](V1_RESULT_EXPORT.md), and [study comparison](V1_STUDY_COMPARISON.md).
Matched original-paper reproduction and release acceptance remain separate from
the accepted adapted single-seed studies. The [architecture guide](V1_OVERVIEW.md)
summarizes current support boundaries.
