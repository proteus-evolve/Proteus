# Repository Guidelines

## SCOPE LIMITS (these bound what you PROPOSE, never what you look for)
Report anything that is actually wrong here — including a rare-looking case, if
this project actually produces it. Then keep the fix in scope:
1. This is not a security paper. Verification is welcome; over-defense is not.
   Unless this project states otherwise, assume a cooperating operator on their
   own machine; if it has a real adversary, it will say so and that scope wins.
2. Do not add hashes, checksums or fingerprints unless the hash replaces a
   materially more expensive operation AND its result changes what happens next.
3. No defensive scaffolding: no feature flags, migration frameworks, compat
   layers or wrappers for cases that do not occur here.
4. No corner-case obsession: exotic encodings, symlink races, RTL text and
   millisecond races are out of scope unless the case is reachable through this
   project's supported use — its documented inputs, its published interface, its
   real data. Reachable is enough; you do not need a reproduction. Constructible
   in principle is not enough.
5. Where judgement is needed, judge. Do not replace it with a scoring table, a
   checklist, or a re-verification loop over something already settled.
6. None of this overrides security, migration, verification or review that the
   user, this project's own conventions, or a higher-priority rule asked for.
   Those were requested; they are the work, not scope creep.
Shapes already seen, for calibration. Examples, not a checklist — a real finding
is not dismissed by resembling one:
  H  hashing every row of two spreadsheets to answer what comparing cells answers
  H  writing checksum files that nothing ever reads
  E  hardening the accounts of an app that has no users and no deployment
  R  auditing your own patch all night while the feature stays unwritten
  R  a reviewer that returns a failing verdict on everything
  O  guards whose justification is the previous guard, not the requirement
And two that look like the above and are not. Report these:
  ✓  a digest that lets you skip re-reading a large file you already have
  ✓  a rare-looking input this project's own documentation example produces
Before running any check, answer: what specific failure would this detect, and
what would I do differently if it occurred? No answer means do not run it.
Say plainly when something is correct. Do not manufacture findings.


## Project Structure & Module Organization

`proteus/` contains the Python package. Core episode and goal contracts live in
`proteus/core/`; harness integrations belong in `proteus/adapters/`; measurement,
sandboxing, benchmarks, and safety auditing have dedicated subpackages. Put automated
tests in `tests/` using the same feature vocabulary as the package. `examples/` holds
runnable demonstrations, `environments/` contains pinned Docker manifests and images,
and `web/` contains the hosted playground plus static site. Design notes and user guides
live in `docs/`; keep images under `docs/assets/` or `web/static/assets/` as appropriate.

## Build, Test, and Development Commands

- `uv pip install -e '.[dev]'` installs Proteus with pytest and Ruff in editable mode.
- `uv run pytest tests/ -q` runs the full test suite used by CI.
- `uv run pytest tests/test_safety_runner.py -q` runs one focused test module.
- `uv run ruff check .` checks Python formatting and lint rules.
- `uv run proteus run --harness minimal --arm neutral --seeds 1 --episodes 2 --out runs/dev`
  exercises the offline pipeline without Docker or an API key.
- `uv run python web/server.py --port 8400` serves the playground locally.

Python 3.10 or newer is required. Some snapshot tests invoke Git; ensure your local Git
user name and email are configured.

For any test or experiment whose claim depends on model selection, susceptibility, or
final-output behavior, load the required provider credentials from the repository-root `.env`
and run the requested live or fixed model. Do not silently substitute a scripted, mock, or
offline model. If the required credential is absent or invalid, report the test as blocked.
Deterministic unit, contract, and mechanism tests remain offline and must be labelled as such.
Never print, copy into result artifacts, or commit values loaded from `.env`.

## Coding Style & Naming Conventions

Use four-space indentation, type hints on public interfaces, and concise docstrings for
non-obvious contracts. Ruff is configured for a 100-character line limit. Use `snake_case` for
modules, functions, and variables; `PascalCase` for classes; and uppercase names for
constants. Keep adapters harness-specific, while framework-wide behavior stays in core
or the relevant measurement/safety package.

## Testing Guidelines

Tests use pytest. Name files `test_<feature>.py` and tests `test_<behavior>()`; prefer
observable behavior and temporary paths over implementation details. Add regression
coverage with every fix and exercise error or `not_evaluated` boundaries for safety
code. Run the focused module while iterating, then the full suite before opening a PR.

## Commit & Pull Request Guidelines

Recent commits follow Conventional Commit style: `feat(safety): ...`, `fix(report): ...`,
`test(safety): ...`, and `docs(safety): ...`. Keep commits focused and use an optional
scope matching the affected package. PRs should explain the motivation and user-visible
effect, list validation commands, link relevant issues or design documents, and call out
configuration or compatibility changes. Include screenshots for changes under
`web/static/`; never commit API keys, local run artifacts, or unpinned environment images.
