# Event policies and worker interventions (P09e)

An event policy is a versioned, pure transition from an observation and typed
state to new state and named worker prompts. The method owns when to observe,
sample time, and apply a decision. Policies do not create a background thread or
call a model. The existing worker runtime executes the resulting intervention.

This adds a reusable control component for CORAL and other asynchronous methods.
It is not the complete CORAL recipe or evidence of real-model benchmark results.

## Recipe and plugin boundary

Declare an `EventPolicySpec` in `RecipeConfig.event_policies`:

```python
EventPolicySpec(
    id="heartbeat",
    gateway="formal",
    operator=PluginSpec(
        plugin=PluginRef(name="proteus.event.cadence", version="1"),
        options={"rules": [
            {"name": "reflect", "trigger": "evaluations", "every": 1,
             "prompt": "Reflect on the released formal result."},
            {"name": "consolidate", "trigger": "evaluations", "every": 10,
             "scope": "global", "prompt": "Consolidate useful shared findings."},
            {"name": "pivot", "trigger": "plateau", "every": 5,
             "epsilon": 0.0, "prompt": "Try a different research direction."},
        ]},
    ),
    resume_policy="native",
)
```

The referenced gateway fixes the worker membership and released evaluation scope.
Member stages must explicitly expose `evaluation` evidence. Hidden evaluations and
worker-supplied scores cannot become observations. Each evaluation event denotes a
complete gateway submission/cohort, not each task row in that cohort.

A third-party plugin registers kind `event_policy`, capability
`event.pure_transition`, an options Contract and a versioned `state_schema`.
Implement `initial_state()` and `advance(observation, state) -> Transition` from
`proteus.events`. Return `EventAction` objects targeting declared members. Plugin
code, options and state schema participate in the dependency lock. Plugins remain
trusted Python code; purity is a contract, not an OS sandbox.

## Method API

```python
events = services.events("heartbeat")
result = services.gateway("formal").step("submission-1", timeout_seconds=20)
result_ref = services.values.publish("proteus.gateway.result@1", result)
decision_ref = events.observe("observation-1", result_ref)
dispatch_refs = events.apply(decision_ref)

# For time rules, the method samples explicitly, including an initial baseline.
tick_ref = events.tick("timer-1", "worker-a")
events.apply(tick_ref)
```

`observe` verifies the result against the gateway's terminal receipt. Its score is
the mean of the complete, released evaluation cohort; any unscored row leaves the
mean unknown. Failed gateway operations advance the observation count but do not
extend plateau history. A completed cohort with an unknown mean adds `None`.
Observing the same result with another operation ID returns the original decision.
Reusing an operation ID with different inputs is an error.

The built-in cadence policy supports worker/global evaluation counts and measured
time, plus per-worker plateaus with epsilon and persisted cooldown. A global rule
targets the worker associated with the triggering observation. Custom policies
may emit actions for other declared members. The generic plateau requires a
finite baseline before missing scores count toward a stall.

Time is accumulated only between monotonic samples from the same controller
invocation. A restart retains already measured seconds, records an unknown gap and
adds zero for that gap. Neither downtime nor replay duration is counted as measured
research time. A time threshold fires at most once per observation and starts its
next interval at that sample; missed intervals do not cause a burst of prompts.

## Recovery and intervention lifecycle

Observations, including their clock samples, have immutable receipts. Decisions
form a typed chain in the existing method journal/Values snapshot, with the prior
state hash, plugin version, formal source, resulting state and actions. Recovery
checks the chain and re-evaluates its pure transitions against the recorded inputs.
An interruption before journal commit reuses the saved clock and source.

`apply` groups actions per worker and saves the original worker generation before
requesting a pause. It waits for a terminal receipt, then starts one generation
with all of that worker's prompts. The stable plan-derived operation ID and the
existing worker ledger prevent duplicate launches. An unconfirmed pause remains
recoverable; it is not treated as a completed intervention. A method cannot finish
with an unfinished intervention or an active worker.

`native` requires the runtime's confirmed session identity. An explicit `fresh`
policy requires `fresh_orientation` and records the fallback reason; it never
silently substitutes for native continuation. A dispatch reference confirms the
new generation was dispatched, not that its work finished. The method still polls
and joins its workers before returning.

## CORAL reference profiles

Load `proteus.methods.coral.policy:register` and select `coral.heartbeat@1` to call
the unmodified heartbeat implementation from CORAL commit
[`0123dfb939b35228cf2c1fde224cd0561e727408`](https://github.com/Human-Agent-Society/CORAL/tree/0123dfb939b35228cf2c1fde224cd0561e727408).
The options use the same count/plateau rule declarations; the reference has no time
trigger. Prompt text is supplied explicitly by the recipe.

- `reference_manager` creates a fresh runner for each observation, matching the
  pinned manager's actual lifecycle. Consequently its class-level plateau cooldown
  does not survive to the next check.
- `persistent_cooldown` persists that state per worker and is an extension.

Both retain the reference function's strict epsilon comparison and its executable
treatment of leading `None` scores. Proteus processes every formal result in
controller order; CORAL's manager can coalesce simultaneous scored attempts into
one local observation. This scheduling difference and the mapping of failed
gateway operations must be reported in any recipe comparison.

Original reflect/consolidate/pivot prompts, the reference source and Apache-2.0
license are packaged with the adapter. The prompts refer to CORAL commands and
filesystem conventions and are preserved as reference material. They require an
explicit adaptation before use with Proteus's mediated worker interface. See the
[reference notice](../proteus/methods/coral/NOTICE.md).

## Validation and runnable example

```sh
python -m proteus.examples.event_interventions --out /tmp/event-study
proteus recipe lock /tmp/event-study/recipe.json \
  --plugin proteus.examples.event_interventions:register \
  --source proteus=. --out /tmp/event-study/lock.json
proteus recipe run /tmp/event-study/recipe.json \
  --plugin proteus.examples.event_interventions:register \
  --lock /tmp/event-study/lock.json \
  --artifact-store /tmp/event-study/artifact-store --out /tmp/event-run
```

The example uses two real local subprocesses with an explicitly offline protocol
fixture. Each submits a candidate, receives independent formal feedback, pauses,
and confirms native continuation with a reflect prompt. It has no model calls and
does not establish a production model-provider bridge or container worker sandbox.

Tests cover count/time/plateau rules, unknown clock gaps, source/operation binding,
fresh/native policies, terminal enforcement and hard exits after input capture,
clock capture, plan capture, pause, launch and dispatch capture. A separately fetched
CORAL checkout checks exact reference bytes and action traces for both profiles,
two workers, both optimization directions, unknown scores and epsilon thresholds.
`tests/check_event_package.py` runs the public CLI and resume outside the checkout
and checks installed reference resources. CI runs it for both wheel and sdist.

The new plugin/data schemas change dependency locks. Existing runs require their
original implementation and lock; they cannot silently resume under this revision.
