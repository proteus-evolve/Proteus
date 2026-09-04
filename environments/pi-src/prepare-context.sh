#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONTEXT=${1:?usage: prepare-context.sh PI_MONO_CHECKOUT}
DATA_ROOT="$CONTEXT/packages/ai/src/providers/data"

if [ ! -f "$CONTEXT/packages/ai/package.json" ]; then
    echo "not a Pi source checkout: $CONTEXT" >&2
    exit 2
fi

rm -rf "$DATA_ROOT"
mkdir -p "$DATA_ROOT"
tar -xf "$SCRIPT_DIR/model-data-v0.84.2.tar" -C "$DATA_ROOT"
cp "$SCRIPT_DIR/boot.sh" "$CONTEXT/.proteus-boot.sh"
printf '%s\n' '914cf1472e715297caa30db4b9535d534a9eb718' > "$CONTEXT/.proteus-source-ref"
