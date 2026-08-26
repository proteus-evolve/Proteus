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
5. Push the annotated release tag. The tag automatically starts `release-smoke`:

   ```bash
   VERSION=0.2.0
   git tag -a "v$VERSION" -m "Proteus v$VERSION"
   git push origin "v$VERSION"
   ```

6. Do not create the GitHub Release until every `release-smoke` job for that tag passes.
7. Publish the GitHub Release from the same tag. `.github/workflows/publish.yml` verifies
   that the tag matches `proteus.__version__`, builds an sdist and wheel in a non-publishing
   job, then uploads them through the protected `pypi` environment using OIDC.
8. Verify `pip install proteus-evolve==<version>` in a fresh environment and check the
   PyPI provenance/attestation before announcing the release.

PyPI files and versions cannot be replaced. If upload verification fails after a version
has reached PyPI, fix the issue and publish a new version; never attempt to reuse the old
version number.
