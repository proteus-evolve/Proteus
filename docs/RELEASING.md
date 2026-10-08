# Releasing Proteus

Proteus releases use a Git tag as the candidate, the pinned-harness `release-smoke`
workflow as the gate, a GitHub Release as the approval boundary, and PyPI Trusted
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

1. Confirm `main` is clean, pushed, and green in `.github/workflows/ci.yml`.
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
   wheel and a working benchmark from the sdist.
5. Before tagging, run `release-smoke` manually against the preparation branch. Configure
   both `DEEPSEEK_API_KEY` (LLM, DSH, Pi) and `CODEX_API_KEY` or `OPENAI_API_KEY` (Codex)
   as repository Actions secrets. `PROTEUS_CODEX_MODEL` is an optional repository variable;
   an empty value uses the pinned Codex source's default model. Missing Codex authentication
   fails the required job; it never silently removes Codex from the release gate.

   ```bash
   gh workflow run release-smoke.yml --ref <preparation-branch>
   ```

   The Codex job builds the pinned `linux/amd64` Rust environment, runs two episodes
   with a required CLI source edit, checks that episode 2 selected the binary pair for
   the edited episode-1 checkpoint, and verifies rejection of a planted compile error.
   Allow up to six hours for the complete image build and offline boundary gates.

6. After the preparation PR and its CI are green and merged, push the annotated release
   tag. The tag automatically starts another `release-smoke` against the exact release:

   ```bash
   VERSION=0.4.0
   git tag -a "v$VERSION" -m "Proteus v$VERSION"
   git push origin "v$VERSION"
   ```

7. Do not create the GitHub Release until every `release-smoke` job for that tag passes:
   offline, minimal, LLM, Pi, DSH, and Codex. A manual run on an older commit does not
   qualify the tag. Use `docs/releases/v0.4.0.md` as the release body for this candidate.
8. Publish the GitHub Release from the same tag. `.github/workflows/publish.yml` verifies
   that the tag matches `proteus.__version__` and that a successful tag-triggered smoke run
   for the exact commit includes every required job, including Codex. It then builds an
   sdist and wheel in a non-publishing
   job, then uploads them through the protected `pypi` environment using OIDC.
9. Verify `pip install proteus-evolve==<version>` in a fresh environment and check the
   PyPI provenance/attestation before announcing the release.

PyPI files and versions cannot be replaced. If upload verification fails after a version
has reached PyPI, fix the issue and publish a new version; never attempt to reuse the old
version number.
