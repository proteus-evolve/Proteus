"""Contract checks for the locally built, keyless Aki runtime image."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Callable

import pytest

from proteus.adapters.aki import AkiHarness
from proteus.adapters.aki_container import MAX_FRAME_BYTES, decode_frame, encode_frame
from proteus.core.disposition import NEUTRAL, Disposition
from proteus.core.episode import private_record_dir
from proteus.sandbox import DockerSandbox, SandboxConfig

AKI_IMAGE = "proteus-env-aki-src:0.1.0"


@pytest.fixture
def aki_source() -> Path:
    """Require the explicit private checkout needed by the image-build acceptance check."""
    configured = os.environ.get("AKI_HARNESS_SRC")
    if not configured:
        pytest.skip("requires AKI_HARNESS_SRC for the private Aki checkout")
    source = Path(configured)
    if not (source / "pyproject.toml").is_file() or not (source / "uv.lock").is_file():
        pytest.skip("AKI_HARNESS_SRC must name an Aki checkout with pyproject.toml and uv.lock")
    return source


@pytest.fixture(scope="module")
def aki_image() -> str:
    inspected = subprocess.run(
        ["docker", "image", "inspect", AKI_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspected.returncode:
        pytest.skip(f"local image {AKI_IMAGE} has not been built")
    return AKI_IMAGE


@pytest.fixture
def derived_aki_image(tmp_path: Path, aki_image: str) -> Callable[[str], str]:
    del aki_image
    tags: list[str] = []

    def build(dockerfile: str) -> str:
        context = tmp_path / uuid.uuid4().hex
        context.mkdir()
        (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        tag = f"proteus-aki-task2-check:{uuid.uuid4().hex}"
        completed = subprocess.run(
            ["docker", "build", "-t", tag, str(context)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        tags.append(tag)
        return tag

    yield build

    for tag in tags:
        subprocess.run(
            ["docker", "image", "rm", "--force", tag],
            capture_output=True,
            text=True,
            check=False,
        )


def run_aki_image(
    image: str,
    action: dict[str, object],
    *,
    mounts: tuple[tuple[Path, str], ...] = (),
) -> dict[str, object]:
    request = {
        "protocol_version": 1,
        "request_id": uuid.uuid4().hex,
        "kind": "request",
        "payload": action,
    }
    argv = ["docker", "run", "--rm", "--network", "none", "-i"]
    for host, container in mounts:
        argv.extend(["-v", f"{host.resolve()}:{container}"])
    argv.append(image)
    completed = subprocess.run(
        argv,
        input=encode_frame(request),
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    terminal = decode_frame(io.BytesIO(completed.stdout), max_bytes=MAX_FRAME_BYTES)
    assert terminal["request_id"] == request["request_id"]
    assert terminal["kind"] == "terminal"
    result = terminal["payload"]
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize("version", [True, 1.0])
def test_aki_image_rejects_non_integer_protocol_version(aki_image, version):
    request = {
        "protocol_version": version,
        "request_id": uuid.uuid4().hex,
        "kind": "request",
        "payload": {"action": "inspect"},
    }

    completed = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "-i", aki_image],
        input=encode_frame(request),
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert b"unsupported Aki container protocol version" in completed.stderr


def run_aki_image_command(
    image: str, *, entrypoint: str, arguments: list[str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            entrypoint,
            image,
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def run_aki_image_verifier(image: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["environments/aki-src/verify-image.sh", image],
        capture_output=True,
        text=True,
        check=False,
    )


def test_aki_image_inspect_action_is_current_and_keyless(aki_image):
    result = run_aki_image(
        aki_image,
        {"protocol_version": 1, "action": "inspect"},
    )

    assert result["aki_version"] == "0.1.0"
    assert result["native_api"] == "persona_gen+runner.config+runner.supervisor"
    assert result["credential_environment_names"] == []


@pytest.mark.docker
def test_aki_image_init_uses_current_neutral_native_api(aki_image, tmp_path):
    run_root = tmp_path / "run-root"
    run_root.mkdir()

    result = run_aki_image(
        aki_image,
        {
            "action": "init",
            "condition": "neutral",
            "seed": 0,
            "episodes": 1,
            "root": "/run",
        },
        mounts=((run_root, "/run"),),
    )

    assert result["action"] == "init"
    assert result["condition"] == "neutral"
    assert result["native_config"]["root"] == "/run"
    assert result["native_config"]["model"] == "glm-5.2"
    assert result["native_config"]["base_url"].startswith("https://")
    assert result["native_config"]["persona"].startswith("run-")
    assert result["native_config"]["max_turns"] == 100
    assert result["episode_config"]["root"] == "/run"
    assert result["episode_config"]["snapshot_dir"] == "/run/harness"
    assert result["episode_config"]["memory_dir"] == "/run/harness/memory"
    assert result["episode_config"]["skills_dir"] == "/run/harness/skills"
    assert result["episode_config"]["tools_dir"] == "/run/harness/tools"
    assert result["episode_config"]["trace_dir"] == "/run/traces"
    assert result["episode_config"]["loop_path"] == "/run/harness/loop.py"
    assert result["episode_config"]["package_dir"] == "/run/harness/aki"
    assert result["episode_config"]["integrity_path"] == "/run/integrity.json"
    assert result["episode_config"]["aki_root"] == "/run/.aki"
    assert result["episode_config"]["persona_dir"] == "/run/.persona"
    expected = (
        "harness/loop.py",
        "harness/permission_policy.py",
        "harness/permission_policy_control.py",
        "harness/aki",
        "harness/memory",
        "harness/skills",
        "harness/tools",
        "traces",
        ".persona",
        ".aki",
        "integrity.json",
        ".snapshot.git",
    )
    assert all((run_root / relative).exists() for relative in expected)


def test_real_aki_model_proxy_rejects_malformed_controller_frame(aki_image, tmp_path):
    run_root = tmp_path / "run-root"
    state_root = tmp_path / "state"
    run_root.mkdir()
    state_root.mkdir()
    initialized = run_aki_image(
        aki_image,
        {
            "action": "init",
            "condition": "neutral",
            "seed": 0,
            "episodes": 1,
            "root": "/run",
        },
        mounts=((run_root, "/run"),),
    )
    request_id = uuid.uuid4().hex
    request = {
        "protocol_version": 1,
        "request_id": request_id,
        "kind": "request",
        "payload": {
            "action": "ordinary_episode",
            "condition": "neutral",
            "seed": 0,
            "episode": 1,
            "model": "gpt-5.6-luna",
            "base_url": "controller://openai-responses",
            "persona": initialized["native_config"]["persona"],
            "max_turns": 20,
            "max_output_tokens": 65_536,
        },
    }
    malformed_response = {
        "protocol_version": 1.0,
        "request_id": "malformed-controller-response",
        "kind": "model_response",
        "payload": {},
    }

    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-i",
            "-v",
            f"{run_root.resolve()}:/workspace/candidate",
            "-v",
            f"{run_root.resolve()}:/workspace/active:ro",
            "-v",
            f"{state_root.resolve()}:/state",
            aki_image,
        ],
        input=encode_frame(request) + encode_frame(malformed_response),
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode != 0
    first = decode_frame(io.BytesIO(completed.stdout), max_bytes=MAX_FRAME_BYTES)
    assert first["kind"] == "model_request"
    assert first["payload"]["model"] == "gpt-5.6-luna"


@pytest.mark.parametrize("condition", AkiHarness.native_conditions)
def test_aki_every_advertised_condition_initializes_through_current_image(
    aki_image, tmp_path, condition
):
    del aki_image  # the adapter consumes the same configured real image
    run_root = tmp_path / "seed-root"
    disposition = (
        NEUTRAL
        if condition == "neutral"
        else Disposition(label=f"native-{condition}", config={"AKI_ARM": condition})
    )
    harness = AkiHarness()
    harness.seed(run_root / "harness", rng_seed=0)

    harness.install_disposition(run_root / "harness", disposition)

    record = json.loads(
        (private_record_dir(run_root) / "aki-native-config.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["supervisor"]["condition"] == condition


def test_aki_selected_adapter_fails_init_when_current_native_api_is_missing(
    derived_aki_image, tmp_path
):
    image = derived_aki_image(
        f"""
FROM {AKI_IMAGE}
RUN mv /opt/aki/experiments/persona_gen /opt/aki/experiments/persona_gen.missing
"""
    )
    harness = AkiHarness(
        sandbox=DockerSandbox(SandboxConfig(image=image, network="none"))
    )
    run_root = tmp_path / "seed-root"
    harness.seed(run_root / "harness", rng_seed=0)

    with pytest.raises((EOFError, RuntimeError), match="output ended before 8 bytes|exited with 1"):
        harness.install_disposition(run_root / "harness", NEUTRAL)


def test_aki_image_source_archive_and_manifest_exclude_tool_caches(aki_image):
    script = """
import json
import tarfile
from pathlib import Path, PurePosixPath

banned = {'.pytest_cache', '.mypy_cache', '.ruff_cache'}
with tarfile.open('/opt/aki-source.tar') as archive:
    archive_paths = [item.name for item in archive.getmembers()]
manifest_paths = Path('/opt/source-manifest.txt').read_text(encoding='utf-8').splitlines()
offenders = [
    f'{source}:{path}'
    for source, paths in (('archive', archive_paths), ('manifest', manifest_paths))
    for path in paths
    if banned.intersection(PurePosixPath(path).parts)
]
print(json.dumps(offenders))
"""
    completed = run_aki_image_command(
        aki_image,
        entrypoint="python",
        arguments=["-c", script],
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []


def test_aki_image_has_git_for_native_snapshot_initialization(aki_image):
    completed = run_aki_image_command(
        aki_image,
        entrypoint="git",
        arguments=["--version"],
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("git version ")


def test_aki_build_fails_when_source_context_enumeration_fails(tmp_path, aki_source):
    commands = tmp_path / "commands"
    commands.mkdir()
    find_command = commands / "find"
    find_command.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    find_command.chmod(0o755)
    docker_command = commands / "docker"
    docker_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker_command.chmod(0o755)
    env = os.environ.copy()
    env["AKI_HARNESS_SRC"] = str(aki_source)
    env["PATH"] = f"{commands}{os.pathsep}{env['PATH']}"

    completed = subprocess.run(
        ["environments/aki-src/build.sh"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode != 0
    assert "enumerate the scrubbed Aki build context" in completed.stderr


def test_aki_verifier_fails_when_source_tree_enumeration_fails(derived_aki_image):
    image = derived_aki_image(
        f"""FROM {AKI_IMAGE}
RUN chmod 000 /opt/aki/tests
USER 65534:65534
"""
    )

    completed = run_aki_image_verifier(image)

    assert completed.returncode != 0


def test_aki_verifier_fails_when_source_archive_is_corrupt(derived_aki_image):
    image = derived_aki_image(
        f"""FROM {AKI_IMAGE}
RUN printf 'not a tar archive' > /opt/aki-source.tar
"""
    )

    completed = run_aki_image_verifier(image)

    assert completed.returncode != 0


def test_aki_verifier_fails_when_source_manifest_is_invalid(derived_aki_image):
    image = derived_aki_image(
        f"""FROM {AKI_IMAGE}
RUN python -c \"from pathlib import Path; Path('/opt/source-manifest.txt').write_bytes(b'\\xff\\xfe')\"
"""
    )

    completed = run_aki_image_verifier(image)

    assert completed.returncode != 0


def test_aki_verifier_rejects_valid_terminal_from_nonzero_container(derived_aki_image):
    terminal_code = """
import json
import sys

value = {
    "protocol_version": 1,
    "request_id": "verify-image",
    "kind": "terminal",
    "payload": {
        "aki_version": "0.1.0",
        "native_api": "persona_gen+runner.config+runner.supervisor",
        "controller_module": "experiments.runner.controller_model",
        "credential_environment_names": [],
        "source_archive_readable": True,
        "source_manifest_readable": True,
    },
}
payload = json.dumps(value, separators=(",", ":")).encode()
sys.stdout.buffer.write(len(payload).to_bytes(8, "big") + payload)
"""
    encoded_code = base64.b64encode(terminal_code.encode()).decode()
    boot = (
        "#!/bin/sh\n"
        f"python -c \"import base64;exec(base64.b64decode('{encoded_code}'))\"\n"
        "exit 23\n"
    )
    encoded_boot = base64.b64encode(boot.encode()).decode()
    image = derived_aki_image(
        f"""FROM {AKI_IMAGE}
RUN python -c \"import base64; open('/usr/local/bin/aki-proteus-boot', 'wb').write(base64.b64decode('{encoded_boot}'))\" \\
 && chmod +x /usr/local/bin/aki-proteus-boot
"""
    )

    completed = run_aki_image_verifier(image)

    assert completed.returncode != 0
