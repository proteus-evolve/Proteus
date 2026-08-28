#!/bin/sh
# Boot dsh from the seed's own source. /workspace/src (when present) is the agent's copy
# of the deepseek-harness monorepo source; the baked tree is made to match it EXACTLY
# (deletions and renames materialize, and entries absent from the workspace are removed),
# rebuilt with the project's own
# toolchain when the source hash changes, and the built CLI is exec'd.
#
# The hash covers length-framed paths AND per-file contents (plus symlink targets): a
# rename, an empty file, a deletion, or a boundary-preserving multi-file edit changes it. An
# untouched ordinary boot takes the pristine fast path with no copying at all. Runtime-image
# publication uses the explicit materialized smoke mode, which exact-syncs even a pristine
# checkpoint before committing it. Build outputs are cached under /state/build keyed by the
# hash and cache protocol. The rest of /state remains private
# session/profile state. The overlay excludes an agent-installed
# node_modules so it cannot shadow the baked dependencies. A changed tree is relinked
# against the image's offline pnpm store with a frozen lockfile before it can build; this
# lets a candidate add workspace packages without silently inheriting stale links, while
# rejecting undeclared or unavailable dependencies without network access.
set -e

# Hash every path and payload as a length-framed record. Concatenating all file contents
# before the path list is ambiguous: {one="ab", two="c"} and {one="a", two="bc"}
# produce the same byte stream. Symlink targets are part of the tree as well.
tree_hash() {
    node - "$1" "${2:-all}" <<'JS'
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const root = path.resolve(process.argv[2]);
const mode = process.argv[3];
const hash = crypto.createHash("sha256");

function isDependencyInput(relative) {
    const basename = path.posix.basename(relative);
    return basename === "package.json"
        || relative === "pnpm-lock.yaml"
        || relative === "pnpm-workspace.yaml"
        || relative === ".npmrc"
        || relative === ".pnpmfile.cjs"
        || relative === ".pnpmfile.mjs"
        || relative === "pnpmfile.cjs"
        || relative === "pnpmfile.mjs"
        || relative.startsWith("patches/");
}

function record(kind, relative, payload) {
    const name = Buffer.from(relative, "utf8");
    hash.update(kind);
    hash.update("\0");
    hash.update(String(name.length));
    hash.update("\0");
    hash.update(name);
    hash.update("\0");
    hash.update(String(payload.length));
    hash.update("\0");
    hash.update(payload);
    hash.update("\0");
}

function walk(directory, relative = "") {
    const entries = fs.readdirSync(directory, {withFileTypes: true}).sort(
        (a, b) => Buffer.compare(Buffer.from(a.name), Buffer.from(b.name))
    );
    for (const entry of entries) {
        if (entry.name === "node_modules") continue;
        const childRelative = relative ? `${relative}/${entry.name}` : entry.name;
        const child = path.join(directory, entry.name);
        const stat = fs.lstatSync(child);
        if (stat.isSymbolicLink()) {
            if (mode === "all" || isDependencyInput(childRelative)) {
                record("L", childRelative, Buffer.from(fs.readlinkSync(child), "utf8"));
            }
        } else if (stat.isDirectory()) {
            walk(child, childRelative);
        } else if (stat.isFile()) {
            if (mode === "all" || isDependencyInput(childRelative)) {
                record(stat.mode & 0o111 ? "X" : "F", childRelative, fs.readFileSync(child));
            }
        } else {
            throw new Error(`unsupported source entry: ${childRelative}`);
        }
    }
}

walk(root);
process.stdout.write(hash.digest("hex") + "\n");
JS
}

# Materialize the workspace tree exactly while retaining only generated dependency trees.
# This closes the gap between `git archive HEAD` (the seed) and a Docker build context that
# happened to contain dirty or untracked files when /opt/src was initially copied. Node is
# used here because POSIX sh cannot safely walk arbitrary supported path names with NUL
# delimiters. Candidate-provided node_modules remains excluded from both source identity and
# materialization.
exact_sync() {
    node - "$1" "$2" <<'JS'
const fs = require("fs");
const path = require("path");

const sourceRoot = path.resolve(process.argv[2]);
const targetRoot = path.resolve(process.argv[3]);

function kind(stat) {
    if (stat.isSymbolicLink()) return "symlink";
    if (stat.isDirectory()) return "directory";
    if (stat.isFile()) return "file";
    throw new Error("unsupported source entry");
}

function copyEntry(source, target, sourceStat) {
    const sourceKind = kind(sourceStat);
    let targetStat = null;
    try {
        targetStat = fs.lstatSync(target);
    } catch (error) {
        if (error.code !== "ENOENT") throw error;
    }
    if (targetStat && kind(targetStat) !== sourceKind) {
        fs.rmSync(target, {recursive: true, force: true});
        targetStat = null;
    }
    if (sourceKind === "directory") {
        if (!targetStat) fs.mkdirSync(target, {recursive: true});
        syncDirectory(source, target);
        return;
    }
    if (targetStat) fs.rmSync(target, {recursive: true, force: true});
    if (sourceKind === "symlink") {
        fs.symlinkSync(fs.readlinkSync(source), target);
        return;
    }
    fs.copyFileSync(source, target);
    fs.chmodSync(target, sourceStat.mode & 0o777);
}

function syncDirectory(source, target) {
    const sourceNames = new Set();
    for (const entry of fs.readdirSync(source, {withFileTypes: true})) {
        if (entry.name === "node_modules") continue;
        sourceNames.add(entry.name);
    }
    for (const entry of fs.readdirSync(target, {withFileTypes: true})) {
        if (entry.name === "node_modules") continue;
        if (!sourceNames.has(entry.name)) {
            fs.rmSync(path.join(target, entry.name), {recursive: true, force: true});
        }
    }
    for (const name of [...sourceNames].sort((a, b) =>
        Buffer.compare(Buffer.from(a), Buffer.from(b)))) {
        const sourcePath = path.join(source, name);
        copyEntry(sourcePath, path.join(target, name), fs.lstatSync(sourcePath));
    }
}

syncDirectory(sourceRoot, targetRoot);
JS
}

if [ "${1:-}" = "--proteus-tree-hash" ]; then
    [ "$#" -eq 2 ] || { echo "usage: $0 --proteus-tree-hash DIR" >&2; exit 2; }
    tree_hash "$2"
    exit 0
fi
if [ "${1:-}" = "--proteus-dependency-hash" ]; then
    [ "$#" -eq 2 ] || { echo "usage: $0 --proteus-dependency-hash DIR" >&2; exit 2; }
    tree_hash "$2" dependencies
    exit 0
fi

FORCE_MATERIALIZE=0
if [ "${1:-}" = "--proteus-materialized-headless-smoke" ]; then
    FORCE_MATERIALIZE=1
    set -- --proteus-headless-smoke
fi

# the container may run as an arbitrary host uid: give npm a writable HOME
export HOME=/tmp
export COREPACK_ENABLE_NETWORK=0
SRC=/opt/src
CLI=$SRC/apps/cli/lib/bin.js
PNPM_STORE=/opt/pnpm-store
BUILD_STATE=/state/build
BUILD_CACHE_VERSION=v2

if [ -d /workspace/src/apps ]; then
    HASH=$(tree_hash /workspace/src)
    if [ "$FORCE_MATERIALIZE" -eq 0 ] \
            && [ "$HASH" = "$(cat /opt/pristine-hash)" ]; then
        :   # untouched source: the baked tree and build are exactly this source
    else
        # Force the executable source to be exactly the checkpoint. This also removes
        # dirty/untracked build-context files which are absent from the archived seed.
        exact_sync /workspace/src "$SRC"
        # Cache hits and rebuilds must start from the same compiled tree. Otherwise a
        # deleted/renamed source can leave a loadable lib/*.js ghost from an earlier boot.
        (cd "$SRC" && {
        # build:lib owns these three source tiers. Native launcher artifacts are baked
        # apparatus and deliberately excluded: the upstream build does not regenerate
        # them, so deleting their lib/ directories would break an otherwise valid boot.
        find apps packages vendor -type d -name lib -not -path '*/node_modules/*' \
            -exec rm -rf {} + 2>/dev/null || true
        find . -name '*.tsbuildinfo' -not -path './node_modules/*' -delete 2>/dev/null || true
        })
        mkdir -p /state "$BUILD_STATE"
        DEP_HASH=$(tree_hash /workspace/src dependencies)
        if [ "$DEP_HASH" != "$(cat /opt/pristine-dependency-hash)" ]; then
            # package.json / workspace topology is part of the evolvable source, while
            # node_modules is image apparatus. Re-resolve in the disposable image tree so
            # a new workspace package gets real runtime links. Frozen + offline means the
            # candidate must keep pnpm-lock.yaml aligned and cannot fetch an unpinned
            # package. Pure code edits retain the baked dependency links and skip this
            # work entirely.
            #
            # pnpm 11 verifies its default release-age policy with registry metadata even
            # under --offline. The candidate lockfile is instead constrained by a frozen
            # manifest match plus the image's immutable, integrity-checked content store;
            # trust-lockfile disables only that network-backed metadata pass.
            if ! (cd "$SRC" && CI=1 pnpm install --offline --frozen-lockfile \
                    --config.trust-lockfile=true \
                    --ignore-scripts --store-dir "$PNPM_STORE" \
                    >/state/last-dependency-check.log 2>&1); then
                echo "self-edited dependency graph is not reproducible from the frozen offline store:" >&2
                tail -30 /state/last-dependency-check.log >&2
                exit 96
            fi
        fi
        CACHE_PATH="$BUILD_STATE/dist-$BUILD_CACHE_VERSION-$HASH.tar"
        if [ -f "$CACHE_PATH" ]; then
            tar -xf "$CACHE_PATH" -m --no-same-permissions -C "$SRC"
        else
            if ! (cd "$SRC" && npm run build:lib >/state/last-build.log 2>&1); then
                echo "self-edited source does not build; tail of the build log:" >&2
                tail -20 /state/last-build.log >&2
                exit 97
            fi
            # Discover outputs only after the evolved tree has been overlaid and built.
            # A directory list captured from the baked baseline omits every package the
            # candidate adds, producing a cache that passes in the build container but
            # cannot cold-start in the next one.
            CACHE_TMP="$BUILD_STATE/.dist-$BUILD_CACHE_VERSION-$HASH.$$.tar"
            (cd "$SRC" && find apps packages vendor \
                -path '*/node_modules' -prune -o \
                \( -type f -o -type l \) -path '*/lib/*' -print0 2>/dev/null \
                | tar -cf "$CACHE_TMP" --null -T -)
            mv "$CACHE_TMP" "$CACHE_PATH"
        fi
    fi
fi

# A version probe exercises argument parsing, not the headless plugin tree. The adapter
# invokes this mode in a *second* container after the build probe, so it also verifies that
# the cached outputs and regenerated workspace links survive a true cold start. An empty,
# isolated DSH_HOME and no provider credential make the expected terminal state a
# MISSING_CREDENTIAL error after successful plugin loading — no model call is possible.
if [ "${1:-}" = "--proteus-headless-smoke" ]; then
    PROBE_ROOT=$(mktemp -d)
    set +e
    (
        unset DEEPSEEK_API_KEY DEEPSEEK_KEY
        export DSH_HOME="$PROBE_ROOT/home"
        mkdir -p "$DSH_HOME"
        node "$CLI" --profile headless "Proteus cold-start validation."
    ) >"$PROBE_ROOT/stdout" 2>"$PROBE_ROOT/stderr"
    PROBE_RC=$?
    set -e
    if [ "$PROBE_RC" -eq 0 ] \
            || grep -q 'MISSING_CREDENTIAL' "$PROBE_ROOT/stdout" "$PROBE_ROOT/stderr"; then
        exit 0
    fi
    echo "self-edited source fails a fresh headless-profile cold start:" >&2
    tail -40 "$PROBE_ROOT/stderr" >&2
    tail -20 "$PROBE_ROOT/stdout" >&2
    exit 98
fi
exec node "$CLI" "$@"
