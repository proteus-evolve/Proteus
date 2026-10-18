# Managed candidate-code protocol (P07b)

`proteus.code_subject@1` executes a candidate class through a JSON RPC boundary.
Prediction uses the existing EvaluationService. A separate
`services.invoke_candidate` operation runs memory updates or another explicitly
allowed method without inventing an evaluation or a score. Both routes use the
same managed model callback and the existing actor budget ledger.

This is a public execution interface needed by Meta-Harness. It does not yet
implement its proposer, domain experiment, full recipe or isolated code backend.

## Candidate interface and state

The default class interface matches Meta-Harness's memory classes:

```python
class Memory:
    def __init__(self, model): ...
    def predict(self, value): ...       # (answer, metadata)
    def learn_from_batch(self, rows): ...
    def get_state(self): ...            # any finite JSON value, including a string
    def set_state(self, state): ...
```

Each invocation imports the exact materialized candidate in a fresh Python
interpreter, constructs the class with a `model(prompt) -> str` callback, restores
explicit state when initialized, invokes one declared method, then serializes the
resulting state. A class may call the model during construction, prediction,
learning or state access; those calls all cross the managed callback.

`CodeOptions` configures the dotted module, optional class name, allowed methods,
state getter/setter names, prediction method and output convention. Without a
class name, exactly one non-abstract class defined in the module must implement
the declared interface. Imported base classes are not selected. Relative imports
inside candidate packages are supported.
The entry module must exist as Python source in the candidate tree, and its
actual imported path is checked; an installed module cannot replace missing
candidate code.
`prediction_output="value"` supports
classes returning a value directly; the default `answer_metadata` expects the
pair used by Meta-Harness and stores its metadata in the trace.

`CodeState` is separate from the immutable code artifact. Its artifact hash binds
memory to the code version that produced it. Uninitialized state is distinct
from an initialized JSON null. Cross-version state reuse is rejected; a future
method needing migration must implement and record an explicit transformation.
The fixture's learned memory changes while its candidate code hash remains fixed.

## Method API

```python
from proteus.execution.code_records import CodeCall, CodeState

candidate = services.initial_candidate()
initial = CodeState(artifact_hash=candidate.artifact.content_hash)
learned = services.invoke_candidate(candidate, CodeCall(
    method="learn_from_batch", state=initial,
    args=([{"input": "training input", "ground_truth": "declared label"}],),
))
# Check learned.status before using the updated state.
memory = services.values.publish("proteus.code.state@1", learned.state)
receipt = services.evaluate(candidate, "validation", inputs={"memory": memory})
```

The subject declares `input_types={"memory": "proteus.code.state@1"}` and binds
its model actor in `SubjectSpec.actor`. The memory DataRef is included in the
existing cohort, cache, trial and recovery identities. Independent evaluation
starts each task from that input memory. Sequence evaluation carries the explicit
returned `code_memory` state. Candidate files and interpreter globals are never
implicitly reused between calls.

Learning arguments are explicit method-owned inputs. The framework does not read
hidden evaluation references or automatically turn test labels into learning
examples. A method/domain recipe must define and audit the training-label source
and its feedback protocol. The official Meta-Harness offline path, for example,
feeds labelled training examples directly into memory learning before validation;
its online path predicts before supplying feedback. The code interface supports
both, but those full domain protocols remain subsequent work.

## Dispatch, limits and diagnostics

The candidate uses a dedicated socket for model requests and terminal results.
Ordinary stdout/stderr are separate diagnostics, not parsed as model requests.
The host validates message types, ordinals, declared methods and bounded JSON
frames, and stores model interactions plus process diagnostics. Default limits
are 4 MiB per frame, 1 MiB combined stdout/stderr, 16 MiB model trace and 1,000 model
requests per invocation; all are explicit recipe options. Changing them changes
the experiment configuration. Binary stdout is decoded with replacement for
inspection; candidate results and memory must be valid finite JSON.

Every callback runs on the framework dispatch thread through the declared actor.
Run/role call limits reserve before dispatch and settle actual reported usage.
Unknown token/cost/call usage remains unknown. A callback failure is an
infrastructure failure, not candidate-authored answer text. Learning operations
consume model calls but no evaluation count. Their ledger entry records the
additional execution time without charging managed actor time a second time.
`CodeResult` preserves the call hash, candidate, state, output, trace, usage and
status. Failed/timed-out calls retain their prior state.

A finite invocation timeout is required. The host enforces a deadline around
candidate I/O and passes the remaining timeout to the model transport. Chat uses
the smaller of this timeout and its configured transport timeout. Transport
socket timeouts are not a guarantee against every possible stalled external
service; the deadline is also rechecked when the call returns. Runtime plugins
must advertise `request.timeout`; chat and the explicit offline echo runtime
currently do. Fresh model calls are required; candidate memory is carried in
CodeState, not in an implicit actor conversation.

The child also watches its host PID and deadline. Normal completion, protocol
failure, timeout and host hard exit terminate its process group. A successful
parent cannot leave inherited stdout/stderr pipes and descendants running
indefinitely. This is process lifecycle management, not containment of malicious
processes that deliberately escape their group.

## Recovery

`code.call` is a journaled operation with a separate `code_invocation` ledger
entry. A settled result can be restored after a crash before journal publication;
its managed actor records are reattached without repeating calls. A committed
operation replays its original output and state. A started invocation without a
settled result remains uncertain and raises `RecoveryRequired`. A model response
alone does not prove the candidate update completed. Recovery does not rerun an
uncertain candidate to guess what happened, nor does it reset its budget.

## Provider boundary

The current provider explicitly requires `proteus.local_json`, POSIX, fresh
materializations and unrestricted host networking. It runs **trusted candidate
code**. The child receives a small environment without inherited credentials,
HOME or Python configuration, but it can still access the host filesystem and
network. Candidate model use must follow the provided callback contract;
malicious direct SDK/network calls cannot be accounted for or prevented by this
host adapter. The declared call budget governs the managed callback.

Do not treat this adapter as the sandbox for an autonomous proposer. An isolated
backend with controlled network/model access and the P07a evidence-only workspace
is still needed before the full Meta-Harness reproduction is complete. Harbor's
existing integration gates continue to run, but do not prove this new local code
protocol executes inside Docker.

## Reference checks and example

The tests verify byte hashes for official Meta-Harness source at
[`8123ccabe2b19fa1123090b2e5f1bacc20963ce8`](https://github.com/stanford-iris-lab/meta-harness/tree/8123ccabe2b19fa1123090b2e5f1bacc20963ce8/reference_examples/text_classification).
They execute the original NoMemory and FewShotAll memory/interface source both
directly and through the child protocol, comparing prompts, outputs and serialized
state. Only the SDK-bearing `llm.py` import is reduced to its actual LLMCallable
Protocol declaration; inference uses the managed fixture callback. These checks
establish interface fidelity, not benchmark performance or full-method fidelity.

An executable local fixture is included:

```sh
python -m proteus.examples.code_memory --out /tmp/code-memory-study
proteus recipe lock /tmp/code-memory-study/recipe.json \
  --plugin proteus.examples.code_memory:register \
  --source "proteus=$PWD" --out /tmp/code-memory-study/lock.json
proteus recipe run /tmp/code-memory-study/recipe.json \
  --plugin proteus.examples.code_memory:register \
  --lock /tmp/code-memory-study/lock.json \
  --artifact-store /tmp/code-memory-study/artifact-store --out /tmp/code-memory-study/run
```

It runs a cold baseline, updates a tiny numeric memory from explicit examples,
then evaluates the same code with its learned state on validation and hidden test
tasks. Seven managed echo calls make zero live model requests. The fixture is an
execution/serialization check, not an RSI benchmark. Wheel and sdist checks run it
and a no-repeat resume outside the source checkout.
