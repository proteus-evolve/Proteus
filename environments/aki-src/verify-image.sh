#!/bin/sh
set -eu

IMAGE=${1:-proteus-env-aki-src:0.1.0}
VERIFY_ROOT=$(mktemp -d)
REQUEST_FRAME="$VERIFY_ROOT/request.frame"
RESULT_FRAME="$VERIFY_ROOT/result.frame"
trap 'rm -rf "$VERIFY_ROOT"' EXIT HUP INT TERM

test "$(docker image inspect "$IMAGE" --format '{{json .Config.Entrypoint}}')" = \
    '["aki-proteus-boot"]'
test "$(docker image inspect "$IMAGE" --format '{{json .Config.Volumes}}')" = "null"

docker run --rm --network none --entrypoint sh "$IMAGE" -c '
set -eu
python -c "import sys; assert sys.version_info[:2] == (3, 12)"
git --version >/dev/null
test -r /opt/aki-source.tar
test -r /opt/source-manifest.txt
test ! -e /opt/aki/.git
test ! -e /opt/aki/.env
test ! -e /opt/aki/.aki
test ! -e /opt/aki/.claude
test ! -e /opt/aki/.venv
test ! -e /opt/aki/outputs
test ! -e /opt/aki/Aki-experiments-data
test ! -e /opt/aki/proteus
test ! -e /tmp/controller.patch
test ! -e /tmp/requirements.txt
test ! -e /Users/liujiaen/Documents/Codes/Aki
test ! -e /Users/liujiaen/Documents/Codes/Proteus
! grep -F "/Users/liujiaen/Documents/Codes/Aki" /proc/self/mountinfo
! grep -F "/Users/liujiaen/Documents/Codes/Proteus" /proc/self/mountinfo
for name in OPENAI_API_KEY ZAI_KEY DEEPSEEK_KEY; do
    if printenv "$name" >/dev/null 2>&1; then
        exit 1
    fi
done
python - <<PY
import os
import tarfile
from pathlib import Path, PurePosixPath

cache_names = {".pytest_cache", ".mypy_cache", ".ruff_cache"}


def enumerate_tree(root: Path) -> list[Path]:
    if not root.is_dir():
        raise RuntimeError(f"source tree root is not a directory: {root}")
    found: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                found.append(path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
    return found


aki_paths = enumerate_tree(Path("/opt/aki"))
proteus_paths = enumerate_tree(Path("/opt/proteus"))
for path in [*aki_paths, *proteus_paths]:
    if path.name == ".env" or path.name.startswith(".env."):
        raise RuntimeError(f"environment file present in image source: {path}")
for path in aki_paths:
    if path.name in cache_names:
        raise RuntimeError(f"tool cache present in image source: {path}")

with tarfile.open("/opt/aki-source.tar", mode="r:*") as archive:
    archive_names = [member.name for member in archive.getmembers()]
for name in archive_names:
    if cache_names.intersection(PurePosixPath(name).parts):
        raise RuntimeError(f"tool cache present in source archive: {name}")

manifest_text = Path("/opt/source-manifest.txt").read_text(encoding="utf-8")
for line_number, line in enumerate(manifest_text.splitlines(), start=1):
    path = PurePosixPath(line)
    if not line or not path.is_absolute():
        raise RuntimeError(f"invalid source manifest entry at line {line_number}")
    try:
        path.relative_to("/opt/aki")
    except ValueError as exc:
        raise RuntimeError(
            f"source manifest entry escapes /opt/aki at line {line_number}"
        ) from exc
    if cache_names.intersection(path.parts):
        raise RuntimeError(f"tool cache present in source manifest: {line}")
PY
'

python3 -c '
import json
import sys

request = json.dumps({
    "protocol_version": 1,
    "request_id": "verify-image",
    "kind": "request",
    "payload": {"action": "inspect"},
}, separators=(",", ":")).encode()
sys.stdout.buffer.write(len(request).to_bytes(8, "big") + request)
' > "$REQUEST_FRAME"

if ! docker run --rm --network none -i "$IMAGE" \
    < "$REQUEST_FRAME" > "$RESULT_FRAME"; then
    echo "network-disabled Aki inspect action failed" >&2
    exit 1
fi

python3 -c '
import json
import sys

with open(sys.argv[1], "rb") as stream:
    header = stream.read(8)
    assert len(header) == 8
    size = int.from_bytes(header, "big")
    assert 0 < size <= 32 * 1024 * 1024
    payload = stream.read(size)
    assert len(payload) == size
    assert stream.read(1) == b""
terminal = json.loads(payload)
assert type(terminal["protocol_version"]) is int
assert terminal["protocol_version"] == 1
assert terminal["request_id"] == "verify-image"
assert terminal["kind"] == "terminal"
result = terminal["payload"]
assert result["aki_version"] == "0.1.0"
assert result["native_api"] == "persona_gen+runner.config+runner.supervisor"
assert result["controller_module"] == "experiments.runner.controller_model"
assert result["credential_environment_names"] == []
assert result["source_archive_readable"] is True
assert result["source_manifest_readable"] is True
' "$RESULT_FRAME"

echo "verified keyless network-disabled Aki image $IMAGE"
