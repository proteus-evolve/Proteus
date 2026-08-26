# pi, from source

The image this directory builds is what makes pi's self-evolution operate on its **real
TypeScript source**: `/opt/src` is a pi-mono checkout with dependencies and a pristine
build baked in, and the entrypoint (`boot.sh`) syncs the agent's copy from
`/workspace/src` over that tree, rebuilds with the project's own toolchain when the
source hash changes (build outputs cached on `/state`), and execs the built CLI. An
untouched copy boots in seconds via the pristine-hash fast path; a broken edit exits 97
with the build log tail — that is the adapter's viability gate.

Rebuild from the pinned source commit, base image, package lock, and checked-in model-data
bundle:

```bash
environments/pi-src/build.sh
environments/pi-src/verify-image.sh proteus-env-pi-src:0.84.2
```

`model-data-v0.84.2.tar` is the complete catalog consumed by Pi's offline build, including
its generation manifest. The build never calls a model-catalog endpoint. `npm ci` remains
bound by Pi's pinned `package-lock.json`, and the Docker base is pinned in the Dockerfile.

The catalog exposes an upstream v0.84.2 TypeScript inference bug because Cloudflare has no
current completions models. The Dockerfile temporarily supplies the explicit provider API
union for compilation. Type parameters are erased from JavaScript; the original upstream
source is restored and byte-compared before `/opt/pi-source.tar` is created. The verification
script checks the pinned source identity, restored source in both `/opt/src` and the staged
tar, the boot tree identity, and the native CLI version.

Attribution: pi-mono (github.com/badlogic/pi-mono), MIT.

Boot semantics (exact tree): tracked files deleted or renamed by the agent are removed from the baked tree before the overlay (the image carries the `git archive` manifest); the source hash covers paths as well as contents, so renames and empty files always re-key the build; the overlay excludes an agent-installed `node_modules`; and a rebuild first removes every build output and `.tsbuildinfo`, so artifacts are derived from the current source and a deleted entry point cannot boot from a stale bundle. An untouched copy boots via the pristine fast path with no copying at all.
