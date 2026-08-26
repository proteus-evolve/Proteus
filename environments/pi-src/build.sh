#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE_REF=914cf1472e715297caa30db4b9535d534a9eb718
SOURCE_URL=https://github.com/badlogic/pi-mono
IMAGE=${PI_IMAGE:-proteus-env-pi-src:0.84.2}
BUILD_ROOT=$(mktemp -d)
CONTEXT="$BUILD_ROOT/pi-mono"
trap 'rm -rf "$BUILD_ROOT"' EXIT HUP INT TERM

git init -q "$CONTEXT"
git -C "$CONTEXT" remote add origin "$SOURCE_URL"
git -C "$CONTEXT" fetch -q --depth 1 origin "$SOURCE_REF"
git -C "$CONTEXT" checkout -q --detach FETCH_HEAD
"$SCRIPT_DIR/prepare-context.sh" "$CONTEXT"
docker build -f "$SCRIPT_DIR/Dockerfile" -t "$IMAGE" "$CONTEXT"
