from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from proteus.adapters.pi import PiHarness
from proteus.adapters.pi_safety import PiPermissionPolicyAdapter
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.sandbox import DockerSandbox
from proteus.safety.permission_adapter import PermissionSnapshotContext
from proteus.safety.taxonomy import SafetyStatus
from proteus.safety.tool_catalog import (
    DISPATCH_PROBE,
    NativeToolCatalog,
)


def _complete_response(url: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        stream = response.read().decode("utf-8")
    return next(
        json.loads(line.removeprefix("data: "))["response"]
        for line in stream.splitlines()
        if line.startswith("data: {") and '"type":"response.completed"' in line
    )


class _CatalogSandbox:
    """Drive the bridge exactly like Pi, while refusing every tool body."""

    def __init__(self, catalogs: tuple[list[dict[str, object]], ...]) -> None:
        self.catalogs = catalogs
        self.commands: list[list[str]] = []
        self.tool_body_runs = 0

    def run(self, run_root, command, env, timeout_s, mounts=(), stop_check=None):
        del run_root, env, timeout_s, stop_check
        self.commands.append(list(command))
        mounts_by_target = {mount[1]: Path(mount[0]) for mount in mounts}
        models = json.loads(
            (mounts_by_target["/tmp/.pi/agent"] / "models.json").read_text(encoding="utf-8")
        )
        base_url = models["providers"]["proteus-openai"]["baseUrl"].replace(
            "host.docker.internal", "127.0.0.1"
        )
        schemas = self.catalogs[min(len(self.commands) - 1, len(self.catalogs) - 1)]
        response = _complete_response(
            f"{base_url}/responses",
            {
                "model": command[command.index("--model") + 1],
                "input": [{"role": "user", "content": "catalog observation"}],
                "tools": schemas,
                "stream": True,
                "store": False,
            },
        )
        assert not any(item["type"] == "function_call" for item in response["output"])
        state = mounts_by_target["/state"]
        session = state / f"catalog-{len(self.commands):03d}.jsonl"
        session.write_text(
            "".join(
                json.dumps(row) + "\n"
                for row in (
                    {
                        "type": "session",
                        "version": 3,
                        "id": f"catalog-{len(self.commands):03d}",
                        "cwd": "/workspace",
                    },
                    {
                        "type": "message",
                        "id": "assistant-terminal",
                        "parentId": "user",
                        "message": {
                            "role": "assistant",
                            "api": "openai-responses",
                            "provider": "proteus-openai",
                            "model": response["model"],
                            "responseId": response["id"],
                            "stopReason": "stop",
                            "content": [{"type": "text", "text": "catalog complete"}],
                        },
                    },
                )
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")


def _context(tmp_path: Path) -> PermissionSnapshotContext:
    artifact_root = tmp_path / "artifacts"
    snapshot_root = tmp_path / "snapshot"
    for subdir in ("notes", "skills", "tools", "src"):
        (snapshot_root / subdir).mkdir(parents=True, exist_ok=True)
    (snapshot_root / "AGENTS.md").write_text("# Pi fixture\n", encoding="utf-8")
    (snapshot_root / "tools" / "file_only.py").write_text(
        "def file_only(): pass\n", encoding="utf-8"
    )
    return PermissionSnapshotContext(
        snapshot=SnapshotRef("pi-catalog", 3, SnapshotRole.CANDIDATE),
        snapshot_root=snapshot_root,
        trial_root=artifact_root / "trials" / "candidate",
        evidence_dir=artifact_root / "trials" / "candidate" / "raw",
        artifact_root=artifact_root,
    )


def _schemas() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "name": "read",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "type": "function",
            "name": "write",
            "description": "Write a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    ]


def test_pi_collects_only_consistent_offered_schemas_without_tool_execution(
    tmp_path: Path,
) -> None:
    sandbox = _CatalogSandbox((_schemas(),))
    adapter = PiPermissionPolicyAdapter(PiHarness(sandbox=sandbox))
    context = _context(tmp_path)

    catalog = adapter.collect_native_tool_catalog(context)

    assert catalog is not None
    assert catalog.loader_id == "pi_ordinary_agent_tool_registry"
    assert [tool.name for tool in catalog.tools] == ["read", "write"]
    assert "file_only" not in {tool.name for tool in catalog.tools}
    assert json.loads(catalog.tools[1].canonical_schema) == _schemas()[1]
    assert (tmp_path / "artifacts" / catalog.raw_catalog_ref).is_file()
    assert all((tmp_path / "artifacts" / tool.raw_schema_ref).is_file() for tool in catalog.tools)
    assert len(sandbox.commands) == 4
    assert sandbox.tool_body_runs == 0
    assert all("--tools" not in command for command in sandbox.commands)
    assert all("--no-extensions" not in command for command in sandbox.commands)
    assert all("--skill" in command for command in sandbox.commands)
    assert adapter.native_tool_catalog_reason(context.snapshot) == ""


def test_pi_catalog_collection_is_cached_by_snapshot(tmp_path: Path) -> None:
    sandbox = _CatalogSandbox((_schemas(),))
    adapter = PiPermissionPolicyAdapter(PiHarness(sandbox=sandbox))
    context = _context(tmp_path)

    first = adapter.collect_native_tool_catalog(context)
    second = adapter.collect_native_tool_catalog(context)

    assert first is second
    assert len(sandbox.commands) == 4


def test_pi_catalog_collection_recollects_when_evidence_context_changes(tmp_path: Path) -> None:
    sandbox = _CatalogSandbox((_schemas(),))
    adapter = PiPermissionPolicyAdapter(PiHarness(sandbox=sandbox))
    first_context = _context(tmp_path / "first")
    second_context = replace(
        _context(tmp_path / "second"),
        snapshot=first_context.snapshot,
    )

    first = adapter.collect_native_tool_catalog(first_context)
    second = adapter.collect_native_tool_catalog(second_context)

    assert first is not None
    assert second is not None
    assert first is not second
    assert (first_context.artifact_root / first.raw_catalog_ref).is_file()
    assert (second_context.artifact_root / second.raw_catalog_ref).is_file()
    assert len(sandbox.commands) == 8


def test_pi_catalog_rejects_inconsistent_ordinary_request_schemas(tmp_path: Path) -> None:
    sandbox = _CatalogSandbox((_schemas(), _schemas()[:1]))
    adapter = PiPermissionPolicyAdapter(PiHarness(sandbox=sandbox))
    context = _context(tmp_path)

    assert adapter.collect_native_tool_catalog(context) is None
    assert adapter.native_tool_catalog_reason(context.snapshot) == (
        "native_tool_catalog_request_inconsistent"
    )


def test_pi_catalog_delta_probe_leaves_only_non_docker_dispatch_not_evaluated(
    tmp_path: Path,
) -> None:
    sandbox = _CatalogSandbox((_schemas(),))
    adapter = PiPermissionPolicyAdapter(PiHarness(sandbox=sandbox))
    context = _context(tmp_path)
    current = adapter.collect_native_tool_catalog(context)
    assert current is not None
    baseline = NativeToolCatalog(
        snapshot=SnapshotRef("pi-catalog", 2, SnapshotRole.ACTIVE),
        loader_id=current.loader_id,
        tools=(),
        raw_catalog_ref=current.raw_catalog_ref,
    )

    coverage = adapter.probe_native_tool_catalog_delta(baseline, current, context)

    assert [item.name for item in coverage] == ["read", "write"]
    assert all(item.probe_status is SafetyStatus.NOT_EVALUATED for item in coverage)
    assert all(item.probe_scope == DISPATCH_PROBE for item in coverage)
    assert all(
        item.probe_reason
        == "network_none_native_dispatch_requires_docker_sandbox"
        for item in coverage
    )
    assert all(
        (context.artifact_root / item.raw_coverage_ref).is_file() for item in coverage
    )
    assert all("network_none_native_dispatch" in item.native_mechanism for item in coverage)


def test_pi_catalog_delta_records_each_schema_without_a_safe_argument_vector(
    tmp_path: Path,
) -> None:
    schema = _schemas()[0]
    schema["parameters"]["required"] = ["path"]
    sandbox = _CatalogSandbox(((schema,),))
    adapter = PiPermissionPolicyAdapter(PiHarness(sandbox=sandbox))
    context = _context(tmp_path)
    current = adapter.collect_native_tool_catalog(context)
    assert current is not None
    baseline = NativeToolCatalog(
        snapshot=SnapshotRef("pi-catalog", 2, SnapshotRole.ACTIVE),
        loader_id=current.loader_id,
        tools=(),
        raw_catalog_ref=current.raw_catalog_ref,
    )

    coverage = adapter.probe_native_tool_catalog_delta(baseline, current, context)

    assert len(coverage) == 1
    assert coverage[0].name == "read"
    assert coverage[0].probe_status is SafetyStatus.NOT_EVALUATED
    assert coverage[0].probe_scope == DISPATCH_PROBE
    assert (
        coverage[0].probe_reason
        == "native_tool_catalog_schema_requires_or_ambiguously_constrains_arguments"
    )
    assert (context.artifact_root / coverage[0].raw_coverage_ref).is_file()


@pytest.mark.docker
def test_pi_catalog_delta_find_probe_uses_fresh_network_none_dispatch(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker is unavailable")
    image_check = subprocess.run(
        ["docker", "image", "inspect", "proteus-env-pi-src:0.84.2"],
        capture_output=True,
        check=False,
    )
    if image_check.returncode != 0:
        pytest.skip("Pi source image is unavailable")
    base_harness = PiHarness(phase_timeout_s=120)
    assert isinstance(base_harness.sandbox, DockerSandbox)
    boot_asset = Path(__file__).resolve().parents[1] / "environments/pi-src/boot.sh"
    harness = PiHarness(
        phase_timeout_s=120,
        sandbox=DockerSandbox(
            replace(
                base_harness.sandbox.config,
                extra_mounts=((str(boot_asset), "/usr/local/bin/pi-boot"),),
            )
        ),
    )
    adapter = PiPermissionPolicyAdapter(harness)
    context = _context(tmp_path)
    # Pi normally exposes its coding subset. This source edit mirrors a real evolution
    # that makes the already-native, read-only `find` registry entry callable; its schema
    # is then collected from the freshly rebuilt ordinary runtime, not invented by the test.
    harness._extract_self_code(context.snapshot_root / "src")
    session_source = context.snapshot_root / "src/packages/coding-agent/src/core/sdk.ts"
    source_text = session_source.read_text(encoding="utf-8")
    assert '["read", "bash", "edit", "write"]' in source_text
    session_source.write_text(
        source_text.replace(
            '["read", "bash", "edit", "write"]',
            '["read", "bash", "edit", "write", "find"]',
            1,
        ),
        encoding="utf-8",
    )
    current = adapter.collect_native_tool_catalog(context)
    assert current is not None, adapter.native_tool_catalog_reason(context.snapshot)
    find = current.by_name().get("find")
    assert find is not None
    assert adapter._catalog_probe_arguments(find) == {"pattern": "__proteus_probe_no_match__"}
    baseline = NativeToolCatalog(
        snapshot=SnapshotRef("pi-catalog", 2, SnapshotRole.ACTIVE),
        loader_id=current.loader_id,
        tools=tuple(tool for tool in current.tools if tool.name != "find"),
        raw_catalog_ref=current.raw_catalog_ref,
    )

    coverage = adapter.probe_native_tool_catalog_delta(baseline, current, context)

    assert len(coverage) == 1
    assert coverage[0].name == "find"
    # The pinned Pi image lacks `fd`; its native handler truthfully reports that the
    # unavailable fallback cannot download it under this probe's required network=none.
    # That is a completed real handler failure, not missing evaluability evidence.
    assert coverage[0].probe_status is SafetyStatus.FAIL
    assert coverage[0].probe_reason == "native_tool_catalog_probe_handler_failed"
    summary = json.loads(
        (context.artifact_root / coverage[0].raw_coverage_ref).read_text(encoding="utf-8")
    )
    assert summary["network"] == "none"
    assert summary["proposal_observed"]
    assert summary["attempt_observed"]
    assert summary["delivery_observed"]
    assert summary["full_catalog_match"]
    assert summary["arguments"] == {"pattern": "__proteus_probe_no_match__"}
    assert summary["exact_arguments_observed"]
    assert summary["status"] == SafetyStatus.FAIL.value
    assert summary["reason"] == "native_tool_catalog_probe_handler_failed"
    assert not summary["completed"]
    assert coverage[0].probe_scope == DISPATCH_PROBE
    # The boot-time compiler workaround belongs only to the container's copied source;
    # the persisted evolved snapshot retains both its intended registry edit and the
    # upstream TypeScript text that triggered the rebuild.
    assert '"find"' in session_source.read_text(encoding="utf-8")
    assert "return createProvider({" in (
        context.snapshot_root
        / "src/packages/ai/src/providers/cloudflare-ai-gateway.ts"
    ).read_text(encoding="utf-8")
