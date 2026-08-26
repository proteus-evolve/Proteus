# Contributing to Proteus

Proteus is a harness-agnostic driver for measuring agent self-evolution. It knows nothing
about any specific agent framework — it talks only to a `HarnessAdapter`, and it scores
tasks through a `BenchTask`. The two highest-value contributions are therefore **new
harness adapters** (evolve another agent framework) and **new benchmarks** (measure under
more goals). Both are single-file, contract-checked, CI-gated additions. This guide shows
the path; see [`ROADMAP.md`](ROADMAP.md) for what's wanted.

## Dev setup

```bash
python -m pip install -e '.[dev]'                # add ',dsh,pi' to work on those adapters
git config --global user.email you@example.com   # snapshot tests need a git identity
git config --global user.name  "Your Name"
```

The gate every change must pass (this is exactly what CI runs):

```bash
ruff check .
pytest tests/ -q
python tests/run_offline.py     # the no-pytest path must stay alive (stdlib + python3)
```

Style: ruff with `line-length = 100`. A handful of pyupgrade rules are intentionally off
so `tests/run_offline.py` can import the package under a stock macOS `python3` (3.9) — see
`[tool.ruff.lint]` in `pyproject.toml`. Don't reach for `X | None` / `collections.abc` in
code that path imports.

## Adding a harness adapter

An adapter is one class implementing seven methods (see the fully-commented
[`proteus/examples/adapter_template.py`](proteus/examples/adapter_template.py) and the contract in
[`proteus/core/adapter.py`](proteus/core/adapter.py)). The framework handles episodes,
snapshots, dispositions, goals, and measurement; you provide the harness-specific glue.

1. **Scaffold** a skeleton from the template:

   ```bash
   python -m proteus.scaffold adapter MyHarness      # -> proteus/adapters/myharness.py
   ```

2. **Implement the TODOs.** In order of effort:
   - `surfaces()` — declare each persistent, agent-editable region (memory / skills /
     tools / code); the measurement layer iterates this manifest.
   - `seed()` / `install_disposition()` / `disposition_fingerprint()` — provisioning and
     the removable perturbation. Reinstalling `NEUTRAL` **must** restore the fingerprint.
   - `run_episode()` / `read_trace()` — run one context-fresh episode against your loop
     and emit a normalized action trace. This is the real work; for a containerized,
     rebuild-from-source harness copy the pattern in
     [`proteus/adapters/dsh.py`](proteus/adapters/dsh.py) /
     [`pi.py`](proteus/adapters/pi.py). For an offline reference, read
     [`proteus/adapters/minimal.py`](proteus/adapters/minimal.py).
   - *Optional attributes* for advanced harnesses (all default safely if omitted):
     `continuity_mode` (`"native"` / `"framework"` / `"none"` — how fresh phases
     continue) and, for a harness whose own edited files change how it runs,
     `staged_activation = True` plus an optional `validate_candidate()` so an edit only
     takes effect the next episode. See the notes in `proteus/core/adapter.py`.

3. **Check the contract.** Free static + provisioning checks run anywhere; `--episode`
   actually runs one neutral episode (may launch containers / call a model):

   ```bash
   proteus check --harness proteus.adapters.myharness:MyHarness
   proteus check --harness proteus.adapters.myharness:MyHarness --episode
   ```

4. **Gate it in CI.**
   - If your adapter provisions **offline** (no Docker, no model, like `minimal`/`llm`),
     add its name to `PURE_ADAPTERS` in
     [`tests/test_conformance.py`](tests/test_conformance.py) (and
     `OFFLINE_EPISODE_ADAPTERS` if its loop runs offline too). It's now checked on every
     push.
   - If it's **containerized or model-backed**, it can't run on stock CI. Validate it
     locally through the same hook contributors use, and note in your PR that you ran it:

     ```bash
     PROTEUS_CHECK_ADAPTER=proteus.adapters.myharness:MyHarness \
       PROTEUS_CHECK_EPISODE=1 pytest tests/test_conformance.py
     ```

5. **(Optional) register a short name** in `proteus/cli.py::_adapter_factory` so users can
   write `--harness myharness` instead of the `module:Class` form.

Prepared container images for a harness are a separate, optional step —
`proteus env scaffold` / `proteus env build`; see [`docs/ADAPTERS.md`](docs/ADAPTERS.md)
and [`environments/README.md`](environments/README.md).

## Adding a benchmark

A benchmark is a `BenchTask`: goal text, a `setup(ws)` that seeds the task workspace, and
a `grade(ws)` that returns an `EvalResult`. See
[`proteus/examples/benchmark_template.py`](proteus/examples/benchmark_template.py), the contract in
[`proteus/bench/task.py`](proteus/bench/task.py), and the shipped
[`proteus/bench/local.py`](proteus/bench/local.py) / `polyglot.py` / `swe.py`.

1. **Scaffold:**

   ```bash
   python -m proteus.scaffold benchmark my_task      # -> proteus/bench/my_task.py
   ```

2. **Implement `setup` and `grade`.** The task workspace (`<run>/task/`) lives **outside**
   the harness snapshot — it is the exercise, not the subject, and only moves forward.
   Always degrade a broken/absent solution to a legible `0.0`, never raise: one bad
   episode is a recorded zero, not a crashed sweep.

   > **Security:** graders execute agent-authored code. Declare a `sandbox` parameter on
   > `grade` and Proteus injects the episode's grader sandbox (`ctx.grader_sandbox`); run
   > the agent's code through [`proteus/bench/sandbox.py`](proteus/bench/sandbox.py)
   > (`run_python`), which never falls back to host Python — this is what `local.py` and
   > `polyglot.py` do. A plain host `subprocess` is acceptable only for a trusted, local
   > benchmark; see the note in `proteus/examples/benchmark_template.py`.

3. **Wire and test.** `as_goal(TASK)` conditions a run on the task; `as_evaluator(TASK)`
   scores without conditioning. Add a small grader test (solved → `1.0`, empty → `0.0`)
   next to the ones in [`tests/test_conformance.py`](tests/test_conformance.py) /
   `tests/test_bench.py`.

## Pull requests

- One adapter or one benchmark per PR keeps review tractable.
- The three gate commands above must pass; say in the PR which heavy/opt-in checks you ran
  locally (Docker adapters, SWE-bench) since CI can't.
- New public behavior needs a test. New contract surface needs a line in the relevant doc
  under `docs/`.
