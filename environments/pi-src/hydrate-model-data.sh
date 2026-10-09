#!/bin/sh
# Run in a pinned pi-mono checkout. Restore the ignored model values from the
# matching published package, rather than regenerating them from live catalogs.
set -eu

PACKAGE=$(node -e 'const p = require("./packages/ai/package.json"); process.stdout.write(`${p.name}@${p.version}`)')
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT HUP INT TERM
npm pack "$PACKAGE" --ignore-scripts --pack-destination "$STAGING" --json > "$STAGING/pack.json"
ARCHIVE=$(node - "$STAGING" <<'JS'
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const directory = process.argv[2];
const expected = JSON.parse(fs.readFileSync("packages/ai/package.json", "utf8"));
const packages = JSON.parse(fs.readFileSync(path.join(directory, "pack.json"), "utf8"));
if (packages.length !== 1) throw new Error("expected one published model package");
const packed = packages[0];
if (packed.name !== expected.name || packed.version !== expected.version ||
    path.basename(packed.filename) !== packed.filename) {
    throw new Error("published model package does not match the source checkout");
}
const archive = path.join(directory, packed.filename);
const integrity = "sha512-" + crypto.createHash("sha512").update(fs.readFileSync(archive)).digest("base64");
if (integrity !== packed.integrity) throw new Error("published model package integrity mismatch");
process.stdout.write(archive);
JS
)
tar -xzf "$ARCHIVE" -C "$STAGING" package/dist/providers/data
rm -rf packages/ai/src/providers/data
cp -R "$STAGING/package/dist/providers/data" packages/ai/src/providers/data
npm run check:model-data
