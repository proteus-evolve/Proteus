#!/bin/bash
set -euo pipefail
if [ "${1:-}" != --proteus-source-hash ]; then
  if [ "$(/usr/local/bin/proteus-codex --proteus-source-hash /workspace/src)" != "$(cat /opt/codex-source.sha256)" ]; then
    echo 'This recovery image supports only its exact fixed source, not source evolution' >&2
    exit 94
  fi
fi
exec /usr/local/bin/proteus-codex "$@"
