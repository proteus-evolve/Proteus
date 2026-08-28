from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import urllib.request
from dataclasses import replace
from pathlib import Path

from proteus.adapters.pi import PiHarness
from proteus.adapters.pi_safety import PiPermissionPolicyAdapter
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.permission_adapter import PermissionSnapshotContext
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS
from proteus.safety.permission_evidence import PermissionComparisonStatus, PermissionCapabilityState
from proteus.safety.permission_executor import compare_permission_case


def _response(url: str, payload: dict[str, object]) -> dict[str, object]:
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


class _PermissionSandbox:
    """Minimal Pi session writer that executes only the adapter's fixed built-ins."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, run_root, command, env, timeout_s, mounts=(), stop_check=None):
        del run_root, env, timeout_s, stop_check
        self.commands.append(list(command))
        mounted = {mount[1]: (Path(mount[0]), len(mount) > 2 and mount[2] == "ro") for mount in mounts}
        models = json.loads(
            (mounted["/tmp/.pi/agent"][0] / "models.json").read_text(encoding="utf-8")
        )
        base_url = models["providers"]["proteus-openai"]["baseUrl"].replace(
            "host.docker.internal", "127.0.0.1"
        )
        model = command[command.index("--model") + 1]
        input_value: list[object] = [{"role": "user", "content": "permission probe"}]
        rows: list[dict[str, object]] = [
            {
                "type": "session",
                "version": 3,
                "id": f"permission-{len(self.commands):03d}",
                "cwd": "/workspace",
            }
        ]
        for turn in range(4):
            response = _response(
                f"{base_url}/responses",
                {
                    "model": model,
                    "input": input_value,
                    "tools": [
                        {"type": "function", "name": "write", "parameters": {}},
                        {"type": "function", "name": "bash", "parameters": {}},
                    ],
                    "stream": True,
                    "store": False,
                },
            )
            output = response["output"]
            if not output or output[0]["type"] != "function_call":
                rows.append(
                    {
                        "type": "message",
                        "id": f"assistant-terminal-{turn}",
                        "parentId": "user",
                        "message": {
                            "role": "assistant",
                            "api": "openai-responses",
                            "provider": "proteus-openai",
                            "model": response["model"],
                            "responseId": response["id"],
                            "stopReason": "stop",
                            "content": [{"type": "text", "text": "done"}],
                        },
                    }
                )
                break
            call = output[0]
            arguments = json.loads(call["arguments"])
            tool_output, is_error = self._run_tool(call["name"], arguments, mounted)
            composite_id = f'{call["call_id"]}|{call["id"]}'
            rows.extend(
                (
                    {
                        "type": "message",
                        "id": f"assistant-tool-{turn}",
                        "parentId": "user",
                        "message": {
                            "role": "assistant",
                            "api": "openai-responses",
                            "provider": "proteus-openai",
                            "model": response["model"],
                            "responseId": response["id"],
                            "stopReason": "toolUse",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "id": composite_id,
                                    "name": call["name"],
                                    "arguments": arguments,
                                }
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "id": f"tool-result-{turn}",
                        "parentId": f"assistant-tool-{turn}",
                        "message": {
                            "role": "toolResult",
                            "toolCallId": composite_id,
                            "toolName": call["name"],
                            "content": [{"type": "text", "text": tool_output}],
                            "isError": is_error,
                        },
                    },
                )
            )
            input_value = [
                {"role": "user", "content": "permission probe"},
                call,
                {"type": "function_call_output", "call_id": call["call_id"], "output": tool_output},
            ]
        else:
            raise AssertionError("permission sequence did not become terminal")
        session = mounted["/state"][0] / f"permission-{len(self.commands):03d}.jsonl"
        session.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    @staticmethod
    def _host_path(path: str, mounted) -> tuple[Path, bool]:
        if path.startswith("/workspace/candidate/"):
            root, _read_only = mounted["/workspace/candidate"]
            return root / path.removeprefix("/workspace/candidate/"), False
        if path.startswith("/workspace/"):
            root, read_only = mounted["/workspace"]
            return root / path.removeprefix("/workspace/"), read_only
        raise AssertionError(f"unexpected Pi path {path}")

    def _run_tool(self, name: str, arguments: dict[str, object], mounted) -> tuple[str, bool]:
        if name == "write":
            target, read_only = self._host_path(str(arguments["path"]), mounted)
            if read_only:
                return "workspace is read-only", True
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(arguments["content"]), encoding="utf-8")
            return "written", False
        if name == "bash":
            tokens = shlex.split(str(arguments["command"]))
            if tokens[:3] == ["rm", "-rf", "--"]:
                target, read_only = self._host_path(tokens[3], mounted)
                if read_only:
                    return "workspace is read-only", True
                shutil.rmtree(target)
                return "deleted", False
            if tokens[:2] == ["printf", "%s"] and tokens[3] == ">":
                target, read_only = self._host_path(tokens[4], mounted)
                if read_only:
                    return "workspace is read-only", True
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(tokens[2], encoding="utf-8")
                return "written", False
        raise AssertionError(f"unexpected Pi built-in operation: {name} {arguments}")


def _context(tmp_path: Path, role: SnapshotRole = SnapshotRole.CANDIDATE) -> PermissionSnapshotContext:
    artifact_root = tmp_path / "artifacts"
    snapshot_root = tmp_path / f"snapshot-{role.value}"
    for name in ("notes", "skills", "tools", "src"):
        (snapshot_root / name).mkdir(parents=True, exist_ok=True)
    (snapshot_root / "AGENTS.md").write_text("# Pi fixture\n", encoding="utf-8")
    return PermissionSnapshotContext(
        snapshot=SnapshotRef("pi-permission", 4, role),
        snapshot_root=snapshot_root,
        trial_root=artifact_root / "trials" / role.value,
        evidence_dir=artifact_root / "trials" / role.value / "raw",
        artifact_root=artifact_root,
    )


def _case(case_id: str):
    return next(case for case in PERMISSION_CASE_SPECS if case.case_id == case_id)


def _run_case(adapter: PiPermissionPolicyAdapter, context: PermissionSnapshotContext, case_id: str):
    case = _case(case_id)
    binding = adapter.bind(case, context)
    assert binding is not None
    protected = adapter.administer(binding, case.protected, None)
    allowed = adapter.administer(binding, case.allowed_control, None)
    return case, binding, replace(protected, canary=adapter.observe_canary(binding, case.protected)), replace(
        allowed, canary=adapter.observe_canary(binding, case.allowed_control)
    )


def test_pi_permission_adapter_exercises_four_native_cases_with_complete_traces(
    tmp_path: Path,
) -> None:
    sandbox = _PermissionSandbox()
    adapter = PiPermissionPolicyAdapter(PiHarness(sandbox=sandbox))
    context = _context(tmp_path)
    expected_protected_effect = {
        "recursive_deletion": True,
        "protected_overwrite": True,
        "workspace_boundary": False,
        "command_execution": True,
    }

    assert adapter.permission_requires_live_channel is False
    assert all(adapter.live_call_cap(case) == 0 for case in PERMISSION_CASE_SPECS)
    for case_id, protected_effect in expected_protected_effect.items():
        case, binding, protected, allowed = _run_case(adapter, context, case_id)

        assert adapter.capability(case, context).state is PermissionCapabilityState.SUPPORTED
        assert binding.protected.native_tool in {"write", "bash"}
        for trace in (protected, allowed):
            assert trace.proposal is not None
            assert trace.decision is not None
            assert trace.decision.value.value == "allow"
            assert trace.decision.source == "pi.native_tool_dispatch.implicit_allow"
            assert trace.attempt_result is not None
            assert trace.delivery is not None
            assert trace.canary is not None and trace.canary.observed
            assert (context.artifact_root / trace.proposal.raw_event_ref).is_file()
            assert (context.artifact_root / trace.delivery.raw_input_ref).is_file()
        assert protected.canary.effect_committed is protected_effect
        assert allowed.canary.effect_committed is True

    assert len(sandbox.commands) == 16
    assert all("--tools" not in command for command in sandbox.commands)
    assert all("--no-extensions" not in command for command in sandbox.commands)


def test_pi_native_protected_overwrite_is_a_real_candidate_failure(tmp_path: Path) -> None:
    sandbox = _PermissionSandbox()
    adapter = PiPermissionPolicyAdapter(PiHarness(sandbox=sandbox))
    active_context = _context(tmp_path / "active", SnapshotRole.ACTIVE)
    candidate_context = _context(tmp_path / "candidate", SnapshotRole.CANDIDATE)
    case, active_binding, active_protected, active_allowed = _run_case(
        adapter, active_context, "protected_overwrite"
    )
    _case_again, candidate_binding, candidate_protected, candidate_allowed = _run_case(
        adapter, candidate_context, "protected_overwrite"
    )
    active_capability = adapter.capability(case, active_context)
    candidate_capability = adapter.capability(case, candidate_context)

    comparison = compare_permission_case(
        active_snapshot=active_context.snapshot,
        candidate_snapshot=candidate_context.snapshot,
        case_spec=case,
        active_capability=active_capability,
        candidate_capability=candidate_capability,
        active_binding=active_binding,
        candidate_binding=candidate_binding,
        active_protected=active_protected,
        active_allowed=active_allowed,
        candidate_protected=candidate_protected,
        candidate_allowed=candidate_allowed,
    )

    assert comparison.comparison_status is PermissionComparisonStatus.FAIL


def test_pi_keeps_policy_mutation_and_inert_tool_minting_not_evaluated(tmp_path: Path) -> None:
    adapter = PiPermissionPolicyAdapter(PiHarness(sandbox=_PermissionSandbox()))
    context = _context(tmp_path)

    for case_id in ("policy_mutation", "tool_skill_capability_minting"):
        case = _case(case_id)
        capability = adapter.capability(case, context)
        assert capability.state is PermissionCapabilityState.UNSUPPORTED
        assert adapter.bind(case, context) is None
