# Aki, built from the configured source checkout

This recipe builds the private Aki 0.1.0 checkout into the local image
`proteus-env-aki-src:0.1.0`. The resulting worker is keyless and runs with Docker network
mode `none`; Proteus remains the controller for later model-backed actions.

Build and verify it locally:

```bash
AKI_HARNESS_SRC=/absolute/path/to/Aki environments/aki-src/build.sh
environments/aki-src/verify-image.sh
```

The build uses the Aki-supported Python 3.12 base. Host uv 0.11.2 exports exact
dependencies from Aki's frozen lockfile, and the image installs that export without an
unlocked dependency resolve before installing the local Aki package with `--no-deps`.
The temporary scrubbed context excludes Git metadata, `.env` files, caches, host virtual
environments, output/run data, and local Proteus artifacts. The checked-in patch adds only
the frozen Unix-socket controller-model selection; when `PROTEUS_AKI_CONTROLLER_SOCKET`
is absent, Aki keeps its native provider behavior.

The image and its private source archive are local acceptance artifacts. Do not push,
publish, export, or commit either one.
