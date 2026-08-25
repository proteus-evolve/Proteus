#!/bin/sh
set -eu

IMAGE=${1:-proteus-env-pi-src:0.84.2}

docker run --rm --network none --entrypoint sh "$IMAGE" -c '
set -eu
test "$(cat /opt/pi-source-ref)" = "914cf1472e715297caa30db4b9535d534a9eb718"
! grep -q "return createProvider<" \
    /opt/src/packages/ai/src/providers/cloudflare-ai-gateway.ts
VERIFY_ROOT=$(mktemp -d)
tar -xf /opt/pi-source.tar --strip-components=1 -C "$VERIFY_ROOT"
cmp /opt/src/packages/ai/src/providers/cloudflare-ai-gateway.ts \
    "$VERIFY_ROOT/packages/ai/src/providers/cloudflare-ai-gateway.ts"
test "$(pi-boot --proteus-tree-hash "$VERIFY_ROOT")" = "$(cat /opt/pristine-hash)"
test "$(node /opt/src/packages/coding-agent/dist/cli.js --version)" = "0.84.2"
'
