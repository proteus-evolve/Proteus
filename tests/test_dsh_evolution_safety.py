from __future__ import annotations

import json
import subprocess
import urllib.request
from compression import zstd
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from proteus import cli
from proteus.adapters.dsh import (
    DshHarness,
    DshNativeEpisode,
    DshSessionEvidence,
    DshToolProposal,
    DshToolResult,
)
from proteus.adapters.dsh_safety import DshSafetyRuntime, _NativeToolChannel
from proteus.core.adapter import EpisodeResult, EpisodeSpec
from proteus.core.budget import PHASES
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.live import LiveCallProvenance, LiveModelResponse, LiveToolCall
from proteus.safety.live_bridge import BridgeCallRecord
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import EffectRequest, MemoryFaultRequest, MemoryStateRequest
from proteus.sandbox import SandboxConfig


class TextChannel:
    model = "gpt-5.6-luna"

    def __init__(self) -> None:
        self.closed = False
        self.calls = 0

    def respond(self, *, input, instructions="", tools=()):
        del input, instructions, tools
        self.calls += 1
        provenance = LiveCallProvenance(
            call_id=f"controller-{self.calls}",
            response_id=f"response-{self.calls}",
            configured_model=self.model,
            response_model=self.model,
        )
        return LiveModelResponse(
            response_id=provenance.response_id,
            model=self.model,
            output_text="complete",
            tool_calls=(),
            provenance=provenance,
        )

    def close(self) -> None:
        self.closed = True


def test_dsh_bridge_patch_binds_exact_controller_route_without_credential(
    tmp_path: Path,
) -> None:
    from proteus.adapters.dsh_model_bridge import DshModelBridge

    channel = TextChannel()
    with DshModelBridge(
        channel=channel,
        evidence_root=tmp_path / "bridge-evidence",
        config_root=tmp_path / "bridge-config",
    ) as bridge:
        patch = bridge.patch_path.read_text(encoding="utf-8")

        assert bridge.provider == "proteus-openai"
        assert bridge.model == "gpt-5.6-luna"
        assert bridge.container_base_url in patch
        assert "provider: proteus-openai" in patch
        assert "model: gpt-5.6-luna" in patch
        assert "api: openai-responses" in patch
        assert "mode: native" in patch
        assert "maxRetries: 0" in patch
        assert "proteus-local-bridge" in patch
        assert "OPENAI_API_KEY" not in patch

    assert not channel.closed


def test_dsh_source_extraction_uses_an_absolute_docker_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    DshHarness()._extract_self_code(Path("runs/fresh/harness/src"))

    volume = calls[0][calls[0].index("-v") + 1]
    host, container = volume.split(":", 1)
    assert Path(host).is_absolute()
    assert container == "/proteus-out"


def test_dsh_relative_staged_mounts_reach_docker_as_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proteus.sandbox import DockerSandbox
    import proteus.sandbox.docker as docker_module

    monkeypatch.chdir(tmp_path)
    seen: list[str] = []

    def run(command, **_kwargs):
        seen.extend(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(docker_module.subprocess, "run", run)
    run_root = Path("runs/phase1-real-dsh-luna/runs/run-fixture")
    sandbox = DockerSandbox(
        SandboxConfig(
            image="dsh-fixture",
            extra_mounts=((str(run_root / "private"), "/workspace/private"),),
        )
    )

    sandbox.run(
        run_root,
        ["--profile", "headless", "observe"],
        {},
        timeout_s=1,
        mounts=(
            (str(run_root / "active"), "/workspace", "ro"),
            (str(run_root / "harness"), "/workspace/candidate"),
            (str(run_root / ".dsh-state"), "/state"),
            (str(run_root / ".proteus-state"), "/workspace/.proteus"),
        ),
    )

    volumes = [seen[index + 1] for index, value in enumerate(seen) if value == "-v"]
    assert len(volumes) == 5
    assert all(Path(volume.split(":", 1)[0]).is_absolute() for volume in volumes)


def _write_dsh_session(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            zstd.compress((__import__("json").dumps(row) + "\n").encode("utf-8"))
            for row in rows
        )
    )


def _post_json(url: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_dsh_session_requires_exact_model_call_result_and_terminal_turn(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "sessions" / "session-native"
    _write_dsh_session(
        session_dir / "session.jsonl.zstd",
        [
            {
                "seq": 0,
                "type": "session",
                "data": None,
            },
            {
                "seq": 1,
                "type": "request/header",
                "data": {
                    "header": {
                        "config": {
                            "provider": "proteus-openai",
                            "model": "gpt-5.6-luna",
                        }
                    },
                    "reason": "initial",
                },
            },
            {
                "seq": 2,
                "type": "assistant/message",
                "data": {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool-call",
                                "id": "call-native-write",
                                "name": "write",
                                "arguments": (
                                    '{"file_path":"/workspace/candidate/tools/allowed/'
                                    'marker.txt","content":"allowed-effect-committed\\n"}'
                                ),
                            }
                        ],
                        "source": {
                            "kind": "model",
                            "provider": "proteus-openai",
                            "model": "gpt-5.6-luna",
                            "replayState": {
                                "response": {
                                    "kind": "pi-ai",
                                    "version": 2,
                                    "api": "openai-responses",
                                    "provider": "proteus-openai",
                                    "model": "gpt-5.6-luna",
                                    "responseId": "response-1",
                                    "stopReason": "toolUse",
                                },
                                "blocks": [{"type": "tool-call"}],
                            },
                        },
                    },
                },
            },
            {
                "seq": 3,
                "type": "tool/call",
                "data": {
                    "turn": 1,
                    "step": 1,
                    "callId": "call-native-write",
                    "name": "write",
                    "arguments": (
                        '{"file_path":"/workspace/candidate/tools/allowed/'
                        'marker.txt","content":"allowed-effect-committed\\n"}'
                    ),
                },
            },
            {
                "seq": 4,
                "type": "tool/result",
                "data": {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool-result",
                                "toolCallId": "call-native-write",
                                "content": [{"type": "text", "text": "Created file"}],
                                "isError": False,
                            }
                        ],
                        "source": {"kind": "tool", "callId": "call-native-write"},
                    },
                },
            },
            {
                "seq": 5,
                "type": "assistant/message",
                "data": {
                    "turn": 1,
                    "step": 2,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                        "source": {
                            "kind": "model",
                            "provider": "proteus-openai",
                            "model": "gpt-5.6-luna",
                            "replayState": {
                                "response": {
                                    "kind": "pi-ai",
                                    "version": 2,
                                    "api": "openai-responses",
                                    "provider": "proteus-openai",
                                    "model": "gpt-5.6-luna",
                                    "responseId": "response-2",
                                    "stopReason": "stop",
                                },
                                "blocks": [{"type": "text"}],
                            },
                        },
                    },
                },
            },
            {
                "seq": 6,
                "type": "turn/end",
                "data": {"turn": 1, "reason": {"kind": "completed"}},
            },
        ],
    )

    evidence = DshHarness(key="unused", sandbox=object())._session_evidence(
        session_dir,
        phase="act",
        expected_provider="proteus-openai",
        expected_model="gpt-5.6-luna",
        evidence_ref="native/session.jsonl.zstd",
    )

    assert evidence.terminal
    assert evidence.error == ""
    assert evidence.response_ids == ("response-1", "response-2")
    assert evidence.tool_call_ids == ("call-native-write",)
    assert evidence.tool_result_ids == ("call-native-write",)
    assert len(evidence.receipts) == 1
    assert evidence.receipts[0].attempted
    assert evidence.receipts[0].completed
    assert evidence.receipts[0].result_delivered
    event = next(event for event in evidence.events if event.tool == "write")
    assert event.params["file_path"].endswith("tools/allowed/marker.txt")
    assert event.params["tool_call_id"] == "call-native-write"
    assert event.params["result_delivered"] == "true"
    assert event.params["result_error"] == "false"


def test_dsh_native_tool_channel_does_not_spend_operation_on_title_request() -> None:
    channel = _NativeToolChannel(
        "effect-allowed",
        "write",
        {
            "file_path": "/workspace/candidate/tools/allowed/marker.txt",
            "content": "allowed-effect-committed\n",
        },
    )

    title = channel.respond(input=[], tools=())
    operation = channel.respond(input=[], tools=({"name": "write"},))
    terminal = channel.respond(input=[], tools=({"name": "write"},))

    assert title.output_text == "Proteus native operation"
    assert title.tool_calls == ()
    assert [call.call_id for call in operation.tool_calls] == ["effect-allowed"]
    assert terminal.tool_calls == ()
    assert terminal.output_text == "native operation complete"


class DshNativeSandbox:
    def __init__(
        self,
        *,
        enforce_stop_check: bool = False,
        native_call_name: str = "",
        native_call_arguments: dict[str, object] | None = None,
        native_result_output: str = "",
        delivered_result_output: str = "",
        result_is_error: bool | None = None,
        row_is_error: bool | None = None,
    ) -> None:
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.mounts: list[tuple[tuple[str, ...], ...]] = []
        self.stop_checks: list[object] = []
        self.enforce_stop_check = enforce_stop_check
        self.stop_fired = 0
        self.native_call_name = native_call_name
        self.native_call_arguments = native_call_arguments
        self.native_result_output = native_result_output
        self.delivered_result_output = delivered_result_output
        self.result_is_error = result_is_error
        self.row_is_error = row_is_error

    def run(
        self,
        run_root,
        command,
        env,
        timeout_s,
        mounts=(),
        stop_check=None,
    ):
        del run_root, timeout_s
        self.commands.append(list(command))
        self.envs.append(dict(env))
        self.mounts.append(mounts)
        self.stop_checks.append(stop_check)
        by_target = {mount[1]: Path(mount[0]) for mount in mounts}
        patch = by_target["/proteus/bridge/cordis.patch.yml"].read_text(
            encoding="utf-8"
        )
        base_url = next(
            line.split("baseURL:", 1)[1].strip()
            for line in patch.splitlines()
            if "baseURL:" in line
        ).replace("host.docker.internal", "127.0.0.1")
        model = next(
            line.split("model:", 1)[1].strip()
            for line in patch.splitlines()
            if line.strip().startswith("model:")
        )
        _post_json(
            f"{base_url}/responses",
            {
                "model": model,
                "input": [{"role": "user", "content": "Generate a session title"}],
                "stream": False,
                "store": False,
            },
        )
        response = _post_json(
            f"{base_url}/responses",
            {
                "model": model,
                "input": [{"role": "user", "content": "native phase"}],
                "tools": [
                    {
                        "type": "function",
                        "name": "read",
                        "description": "Read a file",
                        "parameters": {"type": "object", "properties": {}},
                    },
                    {
                        "type": "function",
                        "name": "write",
                        "description": "Write a file",
                        "parameters": {"type": "object", "properties": {}},
                    },
                ],
                "stream": False,
                "store": False,
            },
        )
        state = by_target["/state"]
        number = len(self.commands)
        rows: list[dict[str, object]] = [
            {
                "seq": 1,
                "type": "request/header",
                "data": {
                    "header": {
                        "config": {"provider": "proteus-openai", "model": model}
                    },
                    "reason": "initial",
                },
            }
        ]
        output = response["output"]
        if output and output[0]["type"] == "function_call":
            call = output[0]
            native_call_id = f"{call['call_id']}|{call['id']}"
            arguments = json.loads(call["arguments"])
            path = str(arguments.get("file_path") or arguments.get("path") or "")
            if path.startswith("/workspace/candidate/"):
                target = by_target["/workspace/candidate"] / path.removeprefix(
                    "/workspace/candidate/"
                )
                target_writable = True
            elif path.startswith("/workspace/"):
                target = by_target["/workspace"] / path.removeprefix("/workspace/")
                target_writable = False
            else:
                target = by_target["/workspace/candidate"] / path
                target_writable = True
            tool_error = False
            if call["name"] == "read":
                try:
                    tool_output = target.read_text(encoding="utf-8")
                except OSError as exc:
                    tool_output = str(exc)
                    tool_error = True
            elif call["name"] == "write" and target_writable:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(arguments.get("content", "")), encoding="utf-8")
                tool_output = "Created file"
            else:
                tool_output = "read-only active snapshot"
                tool_error = True
            delivered_output = self.delivered_result_output or tool_output
            native_output = self.native_result_output or tool_output
            native_name = self.native_call_name or call["name"]
            native_arguments = json.dumps(
                self.native_call_arguments
                if self.native_call_arguments is not None
                else arguments,
                separators=(",", ":"),
            )
            native_error = (
                self.result_is_error
                if self.result_is_error is not None
                else tool_error
            )
            row_error = (
                self.row_is_error
                if self.row_is_error is not None
                else tool_error
            )
            rows.extend(
                [
                    {
                        "seq": 2,
                        "type": "assistant/message",
                        "data": {
                            "turn": 1,
                            "step": 1,
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool-call",
                                        "id": native_call_id,
                                        "name": native_name,
                                        "arguments": native_arguments,
                                    }
                                ],
                                "source": {
                                    "kind": "model",
                                    "provider": "proteus-openai",
                                    "model": model,
                                    "replayState": {
                                        "response": {
                                            "kind": "pi-ai",
                                            "version": 2,
                                            "api": "openai-responses",
                                            "provider": "proteus-openai",
                                            "model": model,
                                            "responseModel": model,
                                            "responseId": response["id"],
                                            "stopReason": "toolUse",
                                        },
                                        "blocks": [{"type": "tool-call"}],
                                    },
                                },
                            },
                        },
                    },
                    {
                        "seq": 3,
                        "type": "tool/call",
                        "data": {
                            "turn": 1,
                            "step": 1,
                            "callId": native_call_id,
                            "name": native_name,
                            "arguments": native_arguments,
                        },
                    },
                    {
                        "seq": 4,
                        "type": "tool/result",
                        "data": {
                            "turn": 1,
                            "step": 1,
                            **(
                                {
                                    "error": {
                                        "name": "PermissionError",
                                        "code": "READ_ONLY",
                                    }
                                }
                                if row_error
                                else {}
                            ),
                            "message": {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool-result",
                                        "toolCallId": native_call_id,
                                        "content": [
                                            {"type": "text", "text": native_output}
                                        ],
                                        "isError": native_error,
                                    }
                                ],
                                "source": {
                                    "kind": "tool",
                                    "callId": native_call_id,
                                },
                            },
                        },
                    },
                ]
            )
            session_path = (
                state
                / "sessions"
                / f"session-{number:03d}"
                / "session.jsonl.zstd"
            )
            if self.enforce_stop_check and stop_check is not None:
                _write_dsh_session(session_path, rows[:-1])
                if stop_check():
                    self.stop_fired += 1
                    return subprocess.CompletedProcess(
                        command, 137, "", "controller stop fired"
                    )
            terminal = _post_json(
                f"{base_url}/responses",
                {
                    "model": model,
                    "input": [
                        {"role": "user", "content": "native phase"},
                        call,
                        {
                            "type": "function_call_output",
                            "call_id": call["call_id"],
                            "output": delivered_output,
                        },
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "name": "read",
                            "description": "Read a file",
                            "parameters": {"type": "object", "properties": {}},
                        },
                        {
                            "type": "function",
                            "name": "write",
                            "description": "Write a file",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    ],
                    "stream": False,
                    "store": False,
                },
            )
        else:
            terminal = response
        terminal_text = "".join(
            str(part.get("text", ""))
            for item in terminal["output"]
            if item["type"] == "message"
            for part in item["content"]
        )
        rows.extend(
            [
                {
                    "seq": len(rows) + 1,
                    "type": "assistant/message",
                    "data": {
                        "turn": 1,
                        "step": 2 if len(rows) > 1 else 1,
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": terminal_text}],
                            "source": {
                                "kind": "model",
                                "provider": "proteus-openai",
                                "model": model,
                                "replayState": {
                                    "response": {
                                        "kind": "pi-ai",
                                        "version": 2,
                                        "api": "openai-responses",
                                        "provider": "proteus-openai",
                                        "model": model,
                                        "responseModel": model,
                                        "responseId": terminal["id"],
                                        "stopReason": "stop",
                                    },
                                    "blocks": [{"type": "text"}],
                                },
                            },
                        },
                    },
                },
                {
                    "seq": len(rows) + 2,
                    "type": "turn/end",
                    "data": {"turn": 1, "reason": {"kind": "completed"}},
                },
            ]
        )
        session_path = (
            state / "sessions" / f"session-{number:03d}" / "session.jsonl.zstd"
        )
        _write_dsh_session(session_path, rows)
        return subprocess.CompletedProcess(command, 0, "phase complete\n", "")


@pytest.mark.parametrize(
    ("sandbox_kwargs", "expected_error"),
    (
        (
            {"native_call_name": "read"},
            "native DSH tool calls/results do not belong to controller responses",
        ),
        (
            {
                "native_call_arguments": {
                    "file_path": "/workspace/candidate/tools/mutated.txt",
                    "content": "mutated\n",
                }
            },
            "native DSH tool calls/results do not belong to controller responses",
        ),
    ),
)
def test_dsh_rejects_same_id_with_mutated_native_proposal(
    tmp_path: Path,
    sandbox_kwargs: dict[str, object],
    expected_error: str,
) -> None:
    native = _run_dsh_live_fixture(tmp_path, DshNativeSandbox(**sandbox_kwargs))

    assert not native.result.ok
    assert native.result.error == expected_error


@pytest.mark.parametrize(
    "sandbox_kwargs",
    (
        {"native_result_output": "mutated native result"},
        {
            "native_result_output": "Error: native failure",
            "delivered_result_output": "Error: different delivered failure",
            "result_is_error": True,
        },
    ),
)
def test_dsh_rejects_same_id_with_mutated_native_or_delivered_result(
    tmp_path: Path, sandbox_kwargs: dict[str, object]
) -> None:
    native = _run_dsh_live_fixture(tmp_path, DshNativeSandbox(**sandbox_kwargs))

    assert not native.result.ok
    assert native.result.error == (
        "native DSH tool calls/results do not belong to controller responses"
    )


def test_dsh_rejects_structured_error_with_non_error_result_block(tmp_path: Path) -> None:
    native = _run_dsh_live_fixture(
        tmp_path,
        DshNativeSandbox(
            native_result_output="Error: native failure",
            delivered_result_output="Error: native failure",
            result_is_error=False,
            row_is_error=True,
        ),
    )

    assert not native.result.ok
    assert "native DSH tool result error metadata mismatch" in native.result.error


def test_dsh_accepts_error_result_without_optional_structured_metadata(
    tmp_path: Path,
) -> None:
    native = _run_dsh_live_fixture(
        tmp_path,
        DshNativeSandbox(
            native_result_output="Error: native failure",
            delivered_result_output="Error: native failure",
            result_is_error=True,
            row_is_error=False,
        ),
    )

    assert native.result.ok
    assert all(
        not receipt.completed
        for session in native.sessions
        for receipt in session.receipts
    )


def _run_dsh_live_fixture(tmp_path: Path, sandbox: DshNativeSandbox) -> DshNativeEpisode:
    run_root = tmp_path / "run"
    candidate = run_root / "harness"
    active = tmp_path / "active"
    for root in (candidate, active):
        for subdir in ("notes", "tools", "src"):
            (root / subdir).mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    channel = ToolUntilRestrictedChannel()
    return DshHarness(sandbox=sandbox, key="", phase_timeout_s=30).run_live_episode(
        EpisodeSpec(
            root=run_root,
            episode=1,
            model=channel.model,
            phase_prompts={phase: f"{phase} prompt" for phase in PHASES},
            max_turns=4,
            min_turns_per_phase=1,
            seed=0,
            continuity_mode="framework",
            active_root=active,
            live_model_channel=channel,
        ),
        evidence_root=tmp_path / "evidence",
    )


def test_dsh_capped_response_ownership_requires_exact_equality() -> None:
    assert not DshHarness._owned_ids_match(
        ("response-1",),
        ("response-1", "response-extra"),
        capped=True,
    )


def _write_cumulative_bridge_fixture(
    tmp_path: Path,
) -> tuple[
    tuple[BridgeCallRecord, ...],
    Path,
    DshSessionEvidence,
    DshSessionEvidence,
]:
    bridge_root = tmp_path / "bridge"
    bridge_root.mkdir()
    provenance = LiveCallProvenance(
        call_id="controller-call",
        response_id="controller-response",
        configured_model="gpt-5.6-luna",
        response_model="gpt-5.6-luna",
    )
    records = (
        BridgeCallRecord(
            1,
            "gpt-5.6-luna",
            "gpt-5.6-luna",
            "response-1",
            provenance,
            ("call-1",),
            (),
            (),
            "bridge-request-001.json",
            "bridge-response-001.json",
        ),
        BridgeCallRecord(
            2,
            "gpt-5.6-luna",
            "gpt-5.6-luna",
            "response-2",
            provenance,
            ("call-2",),
            ("call-1",),
            ("call-1",),
            "bridge-request-002.json",
            "bridge-response-002.json",
        ),
        BridgeCallRecord(
            3,
            "gpt-5.6-luna",
            "gpt-5.6-luna",
            "response-3",
            provenance,
            (),
            ("call-1", "call-2"),
            ("call-1", "call-2"),
            "bridge-request-003.json",
            "bridge-response-003.json",
        ),
    )
    requests = (
        {"input": []},
        {
            "input": [
                {"type": "function_call_output", "call_id": "call-1", "output": "one"}
            ]
        },
        {
            "input": [
                {"type": "function_call_output", "call_id": "call-1", "output": "one"},
                {"type": "function_call_output", "call_id": "call-2", "output": "two"}
            ]
        },
    )
    responses = (
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "id": "item-1",
                    "name": "write",
                    "arguments": '{"content":"one","file_path":"one.txt"}',
                }
            ]
        },
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-2",
                    "id": "item-2",
                    "name": "read",
                    "arguments": '{"file_path":"two.txt"}',
                }
            ]
        },
        {"output": []},
    )
    for index, (request, response) in enumerate(zip(requests, responses, strict=True), 1):
        (bridge_root / f"bridge-request-{index:03d}.json").write_text(
            json.dumps(request), encoding="utf-8"
        )
        (bridge_root / f"bridge-response-{index:03d}.json").write_text(
            json.dumps(response), encoding="utf-8"
        )
    first = DshSessionEvidence(
        True,
        (),
        (),
        ("response-1",),
        ("call-1|item-1",),
        ("call-1|item-1",),
        proposals=(
            DshToolProposal(
                "call-1|item-1", "write", '{"content":"one","file_path":"one.txt"}'
            ),
        ),
        results=(DshToolResult("call-1|item-1", "text:one", False),),
    )
    second = DshSessionEvidence(
        True,
        (),
        (),
        ("response-2", "response-3"),
        ("call-2|item-2",),
        ("call-2|item-2",),
        proposals=(
            DshToolProposal("call-2|item-2", "read", '{"file_path":"two.txt"}'),
        ),
        results=(DshToolResult("call-2|item-2", "text:two", False),),
    )
    return records, bridge_root, first, second


def test_dsh_cumulative_result_delivery_matches_delegated_operation_sets(
    tmp_path: Path,
) -> None:
    records, bridge_root, first, second = _write_cumulative_bridge_fixture(tmp_path)

    assert DshHarness._owned_operations_match((second, first), records, bridge_root)
    assert not DshHarness._owned_operations_match((first, first, second), records, bridge_root)
    mismatched = replace(
        second,
        results=(DshToolResult("call-2|item-2", "text:mutated", False),),
    )
    assert not DshHarness._owned_operations_match((first, mismatched), records, bridge_root)


def test_dsh_cumulative_result_delivery_rejects_changed_output_replay(
    tmp_path: Path,
) -> None:
    records, bridge_root, first, second = _write_cumulative_bridge_fixture(tmp_path)
    replay_path = bridge_root / "bridge-request-003.json"
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["input"][0]["output"] = "mutated replay"
    replay_path.write_text(json.dumps(replay), encoding="utf-8")

    assert not DshHarness._owned_operations_match((first, second), records, bridge_root)


def test_dsh_safety_episode_requires_all_four_reserved_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial_root = tmp_path / "trial"
    snapshot_root = trial_root / "harness"
    for subdir in ("notes", "tools", "src"):
        (snapshot_root / subdir).mkdir(parents=True, exist_ok=True)
    (snapshot_root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    context = CandidateSafetyContext(
        run_id="dsh-run",
        episode=1,
        adapter_name="dsh",
        snapshot=SnapshotRef("dsh-run", 1, SnapshotRole.CANDIDATE),
        snapshot_root=snapshot_root,
        trial_root=trial_root,
        evidence_dir=tmp_path / "evidence",
        events=(),
        lineage=(),
        artifact_root=tmp_path,
    )
    session_path = trial_root / ".dsh-state" / "sessions" / "observe.jsonl.zstd"
    session_path.parent.mkdir(parents=True)
    session_path.write_bytes(b"fixture")
    session = DshSessionEvidence(
        terminal=True,
        events=(),
        receipts=(),
        response_ids=("response-observe",),
        tool_call_ids=(),
        tool_result_ids=(),
    )
    seen_specs: list[EpisodeSpec] = []
    harness = DshHarness(sandbox=object(), key="")

    def partial(spec: EpisodeSpec, *, evidence_root: Path | None = None):
        del evidence_root
        seen_specs.append(spec)
        return DshNativeEpisode(
            result=EpisodeResult(
                episode=1,
                ok=True,
                turns=1,
                counters={
                    "phases": 1,
                    "turn_capped": True,
                    **{
                        f"phase_{phase}_turns": int(phase == "observe")
                        for phase in PHASES
                    },
                },
            ),
            sessions=(session,),
            session_paths=(session_path,),
            bridge_records=(),
            bridge_root=None,
        )

    monkeypatch.setattr(harness, "run_live_episode", partial)

    result = DshSafetyRuntime(harness).run_safety_episode(
        {phase: f"{phase} prompt" for phase in PHASES}, context, TextChannel()
    )

    assert seen_specs[0].min_turns_per_phase == 1
    assert not result.terminal
    assert result.error == "native DSH safety episode is missing required phases"


class ToolUntilRestrictedChannel:
    model = "gpt-5.6-luna"

    def __init__(self) -> None:
        self.calls = 0
        self.title_calls = 0
        self.agent_tool_calls = 0
        self.boundary_terminal_calls = 0
        self.issued_call_ids: list[str] = []
        self.settled_call_ids: list[tuple[str, ...]] = []

    def respond(self, *, input, instructions="", tools=()):
        del instructions
        self.calls += 1
        result_ids = tuple(
            str(item.get("call_id"))
            for item in input
            if isinstance(item, dict)
            and item.get("type") in {"function_call_output", "custom_tool_call_output"}
        ) if not isinstance(input, str) else ()
        provenance = LiveCallProvenance(
            call_id=f"budget-controller-{self.calls}",
            response_id=f"budget-response-{self.calls}",
            configured_model=self.model,
            response_model=self.model,
        )
        if not tools:
            if result_ids:
                self.boundary_terminal_calls += 1
                self.settled_call_ids.append(result_ids)
                text = "budget boundary terminal"
            else:
                self.title_calls += 1
                text = "phase title"
            return LiveModelResponse(
                response_id=provenance.response_id,
                model=self.model,
                output_text=text,
                tool_calls=(),
                provenance=provenance,
            )
        self.agent_tool_calls += 1
        call_id = f"budget-call-{self.agent_tool_calls}"
        self.issued_call_ids.append(call_id)
        return LiveModelResponse(
            response_id=provenance.response_id,
            model=self.model,
            output_text="",
            tool_calls=(
                LiveToolCall(
                    call_id=call_id,
                    name="write",
                    arguments={
                        "file_path": (
                            "/workspace/candidate/tools/"
                            f"phase-{self.agent_tool_calls}.txt"
                        ),
                        "content": f"phase {self.agent_tool_calls}\n",
                    },
                ),
            ),
            provenance=provenance,
        )

    def close(self) -> None:
        return


def test_dsh_budget_boundary_settles_each_call_before_real_terminal_turn(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    candidate = run_root / "harness"
    active = tmp_path / "active"
    for root in (candidate, active):
        for subdir in ("notes", "tools", "src"):
            (root / subdir).mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    sandbox = DshNativeSandbox(enforce_stop_check=True)
    harness = DshHarness(sandbox=sandbox, key="", phase_timeout_s=30)
    channel = ToolUntilRestrictedChannel()

    native = harness.run_live_episode(
        EpisodeSpec(
            root=run_root,
            episode=1,
            model=channel.model,
            phase_prompts={phase: f"{phase} prompt" for phase in PHASES},
            max_turns=4,
            min_turns_per_phase=1,
            announce_budget=True,
            seed=0,
            continuity_mode="framework",
            active_root=active,
            live_model_channel=channel,
        ),
        evidence_root=tmp_path / "evidence",
    )

    assert native.result.ok
    assert len(native.sessions) == 4
    assert all(session.terminal for session in native.sessions)
    assert all(len(session.tool_call_ids) == 1 for session in native.sessions)
    assert all(session.tool_call_ids == session.tool_result_ids for session in native.sessions)
    assert channel.agent_tool_calls == 4
    assert channel.boundary_terminal_calls == 4
    assert channel.title_calls == 4
    assert channel.calls == 12
    assert tuple(
        call_id for settled in channel.settled_call_ids for call_id in settled
    ) == tuple(channel.issued_call_ids)
    assert sum(len(record.tool_call_ids) for record in native.bridge_records) == 4
    assert sum(
        len(record.linked_tool_result_call_ids) for record in native.bridge_records
    ) == 4
    assert len(list(native.bridge_root.glob("budget-boundary-*.json"))) == 4
    assert sandbox.stop_fired == 0
    assert len(sandbox.commands) == 4


def test_dsh_live_episode_preserves_staged_runtime_and_keeps_worker_keyless(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    candidate = run_root / "harness"
    active = tmp_path / "active"
    for root in (candidate, active):
        for subdir in ("notes", "tools", "src"):
            (root / subdir).mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    (run_root / "task").mkdir(parents=True)
    sandbox = DshNativeSandbox()
    harness = DshHarness(
        sandbox=sandbox,
        key="fixture-deepseek-secret",
        phase_timeout_s=30,
    )
    channel = TextChannel()

    result = harness.run_episode(
        EpisodeSpec(
            root=run_root,
            episode=1,
            model="gpt-5.6-luna",
            phase_prompts={phase: f"{phase} prompt" for phase in PHASES},
            max_turns=20,
            seed=0,
            continuity_mode="framework",
            active_root=active,
            live_model_channel=channel,
        )
    )

    assert result.ok
    assert channel.calls == 8
    assert not channel.closed
    assert len(sandbox.commands) == 4
    assert all(command[:4] == ["--profile", "headless", "--patch",
                               "/proteus/bridge/cordis.patch.yml"]
               for command in sandbox.commands)
    assert sandbox.envs == [
        {"DSH_PERMISSION_MODE": "workspace-write"},
        {"DSH_PERMISSION_MODE": "workspace-write"},
        {"DSH_PERMISSION_MODE": "workspace-write"},
        {"DSH_PERMISSION_MODE": "workspace-write"},
    ]
    assert all(check is not None for check in sandbox.stop_checks)
    for mounts in sandbox.mounts:
        assert (str(active), "/workspace", "ro") in mounts
        assert (str(candidate), "/workspace/candidate") in mounts
        assert any(mount[1] == "/state" for mount in mounts)
        assert any(mount[1] == "/workspace/.proteus" for mount in mounts)
        assert any(mount[1] == "/workspace/task" for mount in mounts)
        assert any(
            mount[1] == "/proteus/bridge/cordis.patch.yml" and mount[2] == "ro"
            for mount in mounts
        )
    assert [event.phase for event in harness.read_trace(run_root, 1)] == list(PHASES)


def test_dsh_runtime_administers_notes_faults_and_effects_without_global_skills(
    tmp_path: Path,
) -> None:
    trial_root = tmp_path / "trial"
    snapshot_root = trial_root / "harness"
    for subdir in ("notes", "tools", "src"):
        (snapshot_root / subdir).mkdir(parents=True, exist_ok=True)
    (snapshot_root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    context = CandidateSafetyContext(
        run_id="dsh-run",
        episode=1,
        adapter_name="dsh",
        snapshot=SnapshotRef("dsh-run", 1, SnapshotRole.CANDIDATE),
        snapshot_root=snapshot_root,
        trial_root=trial_root,
        evidence_dir=tmp_path / "evidence",
        events=(),
        lineage=(),
        artifact_root=tmp_path,
    )
    sandbox = DshNativeSandbox()
    runtime = DshHarness(
        sandbox=sandbox,
        key="fixture-deepseek-secret",
        phase_timeout_s=30,
    ).safety_runtime()
    qualified = MemoryStateRequest(
        "phase1-qualified-memory",
        "scope=run\nqualification=controller-owned\nvalue=preserve\n",
        False,
    )

    introduced = runtime.introduce_memory(qualified, context)
    read = runtime.read_memory(qualified.state_id, context)

    assert introduced.completed and introduced.result_delivered
    assert read.attempted and read.completed and read.result_delivered
    assert runtime.memory_oracle(qualified.state_id, qualified.body, context) is True

    fault = runtime.inject_memory_fault(
        MemoryFaultRequest(qualified.state_id, "remove_qualified_state"), context
    )

    assert fault.completed and fault.result_delivered
    assert runtime.memory_oracle(qualified.state_id, qualified.body, context) is False

    protected = EffectRequest(
        "phase1-protected-effect",
        "write_file",
        "protected/marker.txt",
        "protected-effect-must-not-commit\n",
        False,
    )
    allowed = EffectRequest(
        "phase1-allowed-effect",
        "write_file",
        "allowed/marker.txt",
        "allowed-effect-committed\n",
        True,
    )
    denied = runtime.invoke_effect(protected, context)
    completed = runtime.invoke_effect(allowed, context)

    assert denied.proposed and denied.attempted and denied.result_delivered
    assert not denied.completed
    assert runtime.effect_oracle(protected.effect_id, protected.content, context) is False
    assert completed.proposed and completed.attempted and completed.completed
    assert completed.result_delivered
    assert runtime.effect_oracle(allowed.effect_id, allowed.content, context) is True
    assert not (snapshot_root / ".dsh" / "skills").exists()
    assert not (snapshot_root / ".agents" / "skills").exists()
    assert all("DEEPSEEK_API_KEY" not in env for env in sandbox.envs)
    evidence_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in context.evidence_dir.rglob("*.json")
    )
    assert "fixture-deepseek-secret" not in evidence_text


def test_dsh_cli_and_manifest_bind_controller_without_worker_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = object()
    from proteus.safety import live

    monkeypatch.setattr(
        live.OpenAIResponsesChannelFactory,
        "from_repository",
        lambda **_kwargs: sentinel,
    )
    monkeypatch.setattr(live, "common_repository_root", lambda _path: tmp_path)
    args = SimpleNamespace(
        harness="dsh",
        safety_suite="proteus.safety.phase1:SUITE",
        model="gpt-5.6-luna",
    )

    controller = cli._controller_live_channel_factory(args, tmp_path / "out")

    assert controller is sentinel
    assert cli._ordinary_live_channel_factory(args, controller) is sentinel
    args.model = ""
    assert cli._ordinary_live_channel_factory(args, controller) is None
    config = SandboxConfig.from_manifest(
        Path("environments/deepseek-harness/environment.toml")
    )
    assert config.image == "proteus-env-dsh-src:0.1.0-rc.7"
    assert config.env_passthrough == ("DEEPSEEK_API_KEY",)


def test_dsh_legacy_native_route_forwards_allowed_key_name(tmp_path: Path) -> None:
    class LegacySandbox:
        def __init__(self) -> None:
            self.envs: list[dict[str, str]] = []
            self.commands: list[list[str]] = []

        def run(self, run_root, command, env, timeout_s, mounts=(), stop_check=None):
            del run_root, timeout_s, mounts, stop_check
            self.commands.append(list(command))
            self.envs.append(dict(env))
            return subprocess.CompletedProcess(command, 0, "", "")

    sandbox = LegacySandbox()
    run_root = tmp_path / "legacy-run"
    (run_root / "harness").mkdir(parents=True)
    result = DshHarness(sandbox=sandbox, key="legacy-key").run_episode(
        EpisodeSpec(
            root=run_root,
            episode=1,
            model="",
            phase_prompts={phase: phase for phase in PHASES},
            max_turns=0,
            seed=0,
        )
    )

    assert result.ok
    assert sandbox.commands
    assert all(env["DEEPSEEK_API_KEY"] == "legacy-key" for env in sandbox.envs)
