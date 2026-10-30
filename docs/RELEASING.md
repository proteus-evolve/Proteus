# Releasing Proteus

Proteus releases use a Git tag as the candidate, the pinned-harness `release-smoke`
workflow plus recipe/package CI, Harbor and native source integration as the gates, a GitHub Release as the approval boundary, and PyPI Trusted
Publishing for the upload. No long-lived PyPI token is stored in GitHub.

## One-time PyPI setup

Before the first release, sign in to PyPI and add a **pending GitHub publisher** under
account publishing settings with these exact values:

| Field | Value |
|---|---|
| PyPI project name | `proteus-evolve` |
| GitHub owner | `proteus-evolve` |
| Repository | `Proteus` |
| Workflow | `publish.yml` |
| Environment | `pypi` |

The pending publisher creates the project on the first successful upload. It does not
reserve the name before that upload.

## Release checklist

For 1.0 preparation, first close the [execution/recovery review](V1_RELEASE_ACCEPTANCE.md)
and verify the [15-profile historical/current evidence mapping](V1_EXPERIMENT_ACCEPTANCE.md).
Keep implementation tests, historical quality results and final tag gates separate.
Use the [requirement disposition](V1_RELEASE_EVIDENCE_MAP.md) to distinguish stale
checklist entries from still-open evidence. A label such as experimental does not
waive mandatory Combee tradeoff or CORAL sharing/intervention/recovery acceptance;
any scope change must be explicit and approved by maintainers before proceeding.

1. Complete the release acceptance in the implementation plan, including real-model studies and their declared evaluation splits. Confirm `main` is clean, pushed, and green in CI and Harbor. For 1.0, merge feature PRs into `dev/1.0.0` first; integrate into `main` only after acceptance.
2. Set the single version source, `proteus.__version__`, in `proteus/__init__.py`; update
   the README badge, install text, and `docs/releases/v<version>.md`. `pyproject.toml`
   reads this value dynamically, and CI verifies installed metadata matches it.
3. Build locally and validate both distributions:

   ```bash
   python -m build
   python -m twine check dist/*
   ```

4. Smoke-test both installed distributions outside the checkout. This is also enforced in
   CI and the publishing workflow: the scaffolder must generate a working adapter from the
   wheel and a working benchmark from the sdist. The required CI distribution job additionally runs the versioned plugin templates, recipe catalog, native inputs, method/workflow examples, result export and study comparisons outside the checkout.
5. Before tagging, run `release-smoke` and `native-source-integration` manually against the preparation branch. Configure
   both `DEEPSEEK_API_KEY` (LLM, DSH, Pi) and `CODEX_API_KEY` or `OPENAI_API_KEY` (Codex)
   as repository Actions secrets. `PROTEUS_CODEX_MODEL` is an optional repository variable;
   an empty value uses the pinned Codex source's default model. Missing Codex authentication
   fails the required job; it never silently removes Codex from the release gate.

   ```bash
   gh workflow run release-smoke.yml --ref <preparation-branch>
   gh workflow run native-source-integration.yml --ref <preparation-branch>
   ```

   The Codex job builds the pinned `linux/amd64` Rust environment, runs two episodes
   with a required CLI source edit, checks that episode 2 selected the binary pair for
   the edited episode-1 checkpoint, and verifies rejection of a planted compile error.
   Allow up to six hours for the complete image build and offline boundary gates.

   The separate native source gate builds fixed Pi/DSH upstream revisions with the
   production Dockerfiles. A local provider drives actual read/write tools, an
   observable next-episode source change, unchanged completed resume and compiler
   rejection of broken source. This gate uses no provider credentials.

6. After the preparation PR and its CI are green and merged, push the annotated release
   tag. The tag automatically starts `release-smoke`, `ci`, `Harbor integration` and
   `Native source integration` against the exact release:

   ```bash
   VERSION="<accepted-release-version>"
   git tag -a "v$VERSION" -m "Proteus v$VERSION"
   git push origin "v$VERSION"
   ```

7. Wait for all four tag-triggered gates on the exact commit:

   | Workflow | Required successful jobs |
   | --- | --- |
   | `release-smoke.yml` | offline, minimal, LLM, Pi, DSH, Codex |
   | `ci.yml` | Python 3.10–3.14, installed wheel/sdist, pinned AIME/DSPy contract, visualization helpers |
   | `harbor.yml` | pinned Harbor with real Docker trials, persistent workers and circle packing |
   | `native-source-integration.yml` | both pinned Pi and DSH source builds, tool edits, next-episode activation and broken-source rejection |

   Missing, skipped, cancelled, failed or still-running required jobs block upload.
   Branch/PR runs and manual runs do not qualify the release tag. Each workflow
   needs one complete successful run; successful jobs from separate partial runs
   cannot be pooled. Live smoke also validates hashed final adapter input evidence
   for every episode; harness CLI inputs do not claim internal provider-message
   coverage. Use `docs/releases/v<version>.md` as the release body.
8. Publish the GitHub Release from the same tag. `.github/workflows/publish.yml` verifies
   that the tag matches `proteus.__version__` and that all four tag-triggered workflows
   pass on the exact commit. Required jobs must independently match that run ID and
   commit. Unavailable or invalid GitHub evidence blocks upload. It then builds an
   sdist and wheel in a non-publishing
   job, then uploads them through the protected `pypi` environment using OIDC.
9. Verify `pip install proteus-evolve==<version>` in a fresh environment and check the
   PyPI provenance/attestation before announcing the release.

PyPI files and versions cannot be replaced. If upload verification fails after a version
has reached PyPI, fix the issue and publish a new version; never attempt to reuse the old
version number.

## Scope of the automated gates

These gates validate implementation, packaging, pinned reference conformance and
live runtime operation. They do not replace the five method families' real-model
benchmark results, declared baseline comparisons, CORAL sharing analysis, or the
release checklist in the implementation plan. The tag is created only after that
acceptance. Adding these gates does not release 1.0, change its package version,
create a tag, dispatch paid inference or publish to PyPI.
