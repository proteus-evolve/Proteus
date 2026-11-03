#!/bin/sh
# Boot pi from the seed's own source. /workspace/src (when present) is the agent's copy
# of the pi-mono source; the baked tree is made to match it EXACTLY (deletions and
# renames materialize — the baked manifest lists every tracked file, and any not present
# in the workspace is removed before the overlay), rebuilt with the project's own
# toolchain when the source hash changes, and the built CLI is exec'd.
#
# The hash covers length-framed paths AND per-file contents (plus symlink targets): a
# rename, an empty file, a deletion, or a boundary-preserving multi-file edit changes it. An
# untouched copy takes the pristine fast path with no copying at all. Build outputs are
# cached on /state keyed by the hash. The overlay excludes an agent-installed
# node_modules so it cannot shadow the baked dependencies.
set -e

# Keep this byte-for-byte equivalent to dsh-src/boot.sh's tree_hash. Each path and
# payload is length-framed, so moving bytes between files cannot collide; symlink targets
# are included instead of silently disappearing from the source identity.
tree_hash() {
    node - "$1" <<'JS'
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const root = path.resolve(process.argv[2]);
const hash = crypto.createHash("sha256");

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
            record("L", childRelative, Buffer.from(fs.readlinkSync(child), "utf8"));
        } else if (stat.isDirectory()) {
            walk(child, childRelative);
        } else if (stat.isFile()) {
            record(stat.mode & 0o111 ? "X" : "F", childRelative, fs.readFileSync(child));
        } else {
            throw new Error(`unsupported source entry: ${childRelative}`);
        }
    }
}

walk(root);
process.stdout.write(hash.digest("hex") + "\n");
JS
}

if [ "${1:-}" = "--proteus-tree-hash" ]; then
    [ "$#" -eq 2 ] || { echo "usage: $0 --proteus-tree-hash DIR" >&2; exit 2; }
    tree_hash "$2"
    exit 0
fi

# the container may run as an arbitrary host uid: give npm a writable HOME
PROTEUS_EXPECTED_SOURCE=''
if [ "${1:-}" = "--proteus-expect-source" ]; then
    PROTEUS_EXPECTED_SOURCE=${2:-}
    case "$PROTEUS_EXPECTED_SOURCE" in ''|*[!0-9a-f]*) exit 2;; esac
    [ "${#PROTEUS_EXPECTED_SOURCE}" -eq 64 ] || exit 2
    shift 2
    [ -d /workspace/src ] || { echo "expected source surface is missing" >&2; exit 97; }
fi
export HOME=/tmp
SRC=/opt/src
CLI=$SRC/packages/coding-agent/dist/cli.js
DISTS="packages/tui/dist packages/telemetry/dist packages/ai/dist packages/agent/dist \
packages/session-backends/sqlite-node/dist packages/protocol/dist packages/client/dist \
packages/server/dist packages/coding-agent/dist"

if [ -e /workspace/src ] || [ -L /workspace/src ]; then
    [ -d /workspace/src/packages ] || { echo "source package tree is missing; refusing baked fallback" >&2; exit 97; }
    HASH=$(tree_hash /workspace/src)
    [ -z "$PROTEUS_EXPECTED_SOURCE" ] || [ "$HASH" = "$PROTEUS_EXPECTED_SOURCE" ] \
        || { echo "source differs from the controller dispatch" >&2; exit 97; }
    PROTEUS_BOOT_MODE=pristine
    if [ "$HASH" = "$(cat /opt/pristine-hash)" ]; then
        :   # untouched source: the baked tree and build are exactly this source
    else
        # materialize deletions/renames of tracked files, then overlay the workspace
        if [ -f /opt/source-manifest.txt ]; then
            while IFS= read -r p; do
                [ -n "$p" ] || continue
                [ -e "/workspace/src/$p" ] || rm -f "$SRC/$p"
            done < /opt/source-manifest.txt
        fi
        # files-only archive: a directory ENTRY makes tar chmod/utime that directory
        # on extraction, which a non-root uid cannot do to the baked root-owned tree.
        # File entries create missing parents quietly and touch nothing that exists.
        (cd /workspace/src && find . -name node_modules -prune -o \
            \( -type f -o -type l \) -print0 | tar -cf - --null -T -) \
            | tar -xf - -m --no-same-permissions -C "$SRC"
        # A cache hit must erase the same outputs as a rebuild, or removed sources leave
        # stale compiled modules behind and the recorded tree differs from what executes.
        (cd "$SRC" && {
        rm -rf $DISTS
        find . -name '*.tsbuildinfo' -not -path './node_modules/*' -delete 2>/dev/null || true
        })
        if [ -f "/state/dist-$HASH.tar" ]; then
            PROTEUS_BOOT_MODE=cache
            tar -xf "/state/dist-$HASH.tar" -m --no-same-permissions -C "$SRC"
        else
            PROTEUS_BOOT_MODE=build
            if ! (cd "$SRC" && npm run build:offline >/state/last-build.log 2>&1); then
                echo "self-edited source does not build; tail of the build log:" >&2
                tail -20 /state/last-build.log >&2
                exit 97
            fi
            mkdir -p /state
            (cd "$SRC" && find $DISTS -type f -print0 \
                | tar -cf "/state/dist-$HASH.tar" --null -T -)
        fi
    fi
fi
if [ -n "$PROTEUS_EXPECTED_SOURCE" ]; then
    node - "$HASH" "$CLI" "$0" "$PROTEUS_BOOT_MODE" <<'JS'
const fs = require('fs'), crypto = require('crypto');
const digest = p => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
process.stdout.write('PROTEUS_RUNTIME_V1 '+JSON.stringify({version:1, backend:'pi',
    source_sha256:process.argv[2], cli_sha256:digest(process.argv[3]),
    boot_sha256:digest(process.argv[4]), mode:process.argv[5]})+'\n');
JS
fi
exec node "$CLI" "$@"
