from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from proteus import cli
from proteus.adapters.pi import PiHarness
from proteus.core.adapter import EpisodeSpec
from proteus.core.budget import PHASES
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.live import (
    LiveCallProvenance,
    LiveModelResponse,
    LiveToolCall,
)
from proteus.safety.live_bridge import OpenAICompatibleBridge
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import (
    EffectRequest,
    MemoryFaultRequest,
    MemoryStateRequest,
    RuntimeKind,
)
from proteus.sandbox import SandboxConfig


class RecordingChannel:
    model = "gpt-5.6-luna"

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.closed = False

    def respond(self, *, input, instructions="", tools=()):
        self.requests.append(
            {"input": input, "instructions": instructions, "tools": list(tools)}
        )
        number = len(self.requests)
        provenance = LiveCallProvenance(
            call_id=f"controller-{number}",
            response_id=f"resp-controller-{number}",
            configured_model=self.model,
            response_model=self.model,
        )
        return LiveModelResponse(
            response_id=provenance.response_id,
            model=self.model,
            output_text="complete" if number == 2 else "",
            tool_calls=(
                LiveToolCall(
                    call_id="call-native-write",
                    name="write",
                    arguments={"path": "notes/kept.md", "content": "kept\n"},
                ),
            )
            if number == 1
            else (),
            provenance=provenance,
        )

    def close(self) -> None:
        self.closed = True


class TextChannel(RecordingChannel):
    def respond(self, *, input, instructions="", tools=()):
        self.requests.append(
            {"input": input, "instructions": instructions, "tools": list(tools)}
        )
        number = len(self.requests)
        provenance = LiveCallProvenance(
            call_id=f"controller-{number}",
            response_id=f"resp-controller-{number}",
            configured_model=self.model,
            response_model=self.model,
        )
        return LiveModelResponse(
            response_id=provenance.response_id,
            model=self.model,
            output_text=f"phase {number} complete",
            tool_calls=(),
            provenance=provenance,
        )


def _post_json(url: str, payload: dict[str, object]) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.headers.get_content_type(), response.read().decode("utf-8")


def test_bridge_preserves_native_request_and_tool_result_ownership(
    tmp_path: Path,
) -> None:
    channel = RecordingChannel()
    tools = [
        {
            "type": "function",
            "name": "write",
            "description": "Write a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        }
    ]
    first_input = [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "write the controlled note"}],
        }
    ]

    with OpenAICompatibleBridge(
        channel=channel,
        evidence_root=tmp_path / "bridge-evidence",
    ) as bridge:
        content_type, first_stream = _post_json(
            f"{bridge.host_base_url}/responses",
            {
                "model": "gpt-5.6-luna",
                "input": first_input,
                "tools": tools,
                "stream": True,
                "store": False,
            },
        )
        second_input = [
            *first_input,
            {
                "type": "function_call",
                "call_id": "call-native-write",
                "name": "write",
                "arguments": '{"path":"notes/kept.md","content":"kept\\n"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call-native-write",
                "output": "Wrote notes/kept.md",
            },
        ]
        _, second_stream = _post_json(
            f"{bridge.host_base_url}/responses",
            {
                "model": "gpt-5.6-luna",
                "input": second_input,
                "tools": tools,
                "stream": True,
                "store": False,
            },
        )

        assert content_type == "text/event-stream"
        assert "response.function_call_arguments.done" in first_stream
        assert '"call_id":"call-native-write"' in first_stream.replace(" ", "")
        assert "response.completed" in second_stream
        assert channel.requests == [
            {"input": first_input, "instructions": "", "tools": tools},
            {"input": second_input, "instructions": "", "tools": tools},
        ]
        assert [record.requested_model for record in bridge.records] == [
            "gpt-5.6-luna",
            "gpt-5.6-luna",
        ]
        assert bridge.records[1].tool_result_call_ids == ("call-native-write",)
        assert bridge.records[1].linked_tool_result_call_ids == ("call-native-write",)

    ledgers = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "bridge-evidence").glob("*.json")
    }
    assert ledgers["bridge-request-002.json"]["input"] == second_input
    assert ledgers["bridge-response-001.json"]["configured_model"] == "gpt-5.6-luna"
    assert ledgers["bridge-response-001.json"]["requested_model"] == "gpt-5.6-luna"
    assert ledgers["bridge-response-001.json"]["returned_model"] == "gpt-5.6-luna"


def test_pi_session_requires_exact_native_call_result_and_terminal_model(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session.jsonl"
    rows = [
        {
            "type": "session",
            "version": 3,
            "id": "session-native",
            "cwd": "/workspace",
        },
        {
            "type": "message",
            "id": "assistant-tool",
            "parentId": "user",
            "message": {
                "role": "assistant",
                "api": "openai-responses",
                "provider": "proteus-openai",
                "model": "gpt-5.6-luna",
                "responseId": "resp-controller-1",
                "stopReason": "toolUse",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call-native-write|fc_resp-controller-1_0",
                        "name": "write",
                        "arguments": {
                            "path": "/workspace/candidate/tools/allowed/marker.txt",
                            "content": "allowed-effect-committed\n",
                        },
                    }
                ],
            },
        },
        {
            "type": "message",
            "id": "tool-result",
            "parentId": "assistant-tool",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-native-write|fc_resp-controller-1_0",
                "toolName": "write",
                "content": [{"type": "text", "text": "Wrote file"}],
                "isError": False,
            },
        },
        {
            "type": "message",
            "id": "assistant-terminal",
            "parentId": "tool-result",
            "message": {
                "role": "assistant",
                "api": "openai-responses",
                "provider": "proteus-openai",
                "model": "gpt-5.6-luna",
                "responseId": "resp-controller-2",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "done"}],
            },
        },
    ]
    session.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    evidence = PiHarness()._session_evidence(
        session,
        phase="act",
        expected_provider="proteus-openai",
        expected_model="gpt-5.6-luna",
        evidence_ref="native/session.jsonl",
    )

    assert evidence.terminal
    assert evidence.error == ""
    assert evidence.response_ids == ("resp-controller-1", "resp-controller-2")
    assert len(evidence.receipts) == 1
    receipt = evidence.receipts[0]
    assert receipt.operation_id == "call-native-write|fc_resp-controller-1_0"
    assert receipt.proposed and receipt.attempted and receipt.completed
    assert receipt.result_delivered
    assert receipt.evidence_refs == ("native/session.jsonl",)
    tool_event = next(event for event in evidence.events if event.tool == "write")
    assert tool_event.params == {
        "path": "/workspace/candidate/tools/allowed/marker.txt",
        "content": "allowed-effect-committed\n",
        "tool_call_id": "call-native-write|fc_resp-controller-1_0",
        "result_delivered": "true",
        "result_error": "false",
    }


class NativeSessionSandbox:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.mounts: list[tuple[tuple[str, ...], ...]] = []

    def run(
        self,
        run_root,
        command,
        env,
        timeout_s,
        mounts=(),
        stop_check=None,
    ):
        del run_root, timeout_s, stop_check
        self.commands.append(list(command))
        self.envs.append(dict(env))
        self.mounts.append(mounts)
        by_target = {mount[1]: Path(mount[0]) for mount in mounts}
        config = json.loads(
            (by_target["/tmp/.pi/agent"] / "models.json").read_text(encoding="utf-8")
        )
        provider = config["providers"]["proteus-openai"]
        content_type, stream = _post_json(
            f'{provider["baseUrl"]}/responses'.replace(
                "host.docker.internal", "127.0.0.1"
            ),
            {
                "model": command[command.index("--model") + 1],
                "input": [{"role": "user", "content": "native phase"}],
                "stream": True,
                "store": False,
            },
        )
        assert content_type == "text/event-stream"
        completed = next(
            json.loads(line.removeprefix("data: "))
            for line in stream.splitlines()
            if line.startswith("data: {") and '"type":"response.completed"' in line
        )
        response = completed["response"]
        state = by_target["/state"]
        session = state / f"session-{len(self.commands):03d}.jsonl"
        rows: list[dict[str, object]] = [
            {
                "type": "session",
                "version": 3,
                "id": f"session-{len(self.commands):03d}",
                "cwd": "/workspace",
            },
        ]
        output = response["output"]
        if output and output[0]["type"] == "function_call":
            call = output[0]
            arguments = json.loads(call["arguments"])
            target = str(arguments.get("path", ""))
            if target.startswith("/workspace/candidate/"):
                host_target = by_target["/workspace/candidate"] / target.removeprefix(
                    "/workspace/candidate/"
                )
            elif target.startswith("/workspace/"):
                host_target = by_target["/workspace"] / target.removeprefix("/workspace/")
            else:
                host_target = by_target["/workspace/candidate"] / target
            tool_name = call["name"]
            is_error = False
            if tool_name == "write":
                host_target.parent.mkdir(parents=True, exist_ok=True)
                host_target.write_text(str(arguments.get("content", "")), encoding="utf-8")
                tool_output = f"Wrote {target}"
            elif tool_name == "read":
                try:
                    tool_output = host_target.read_text(encoding="utf-8")
                except OSError as exc:
                    tool_output = str(exc)
                    is_error = True
            else:
                tool_output = f"unsupported test tool: {tool_name}"
                is_error = True
            composite_id = f'{call["call_id"]}|{call["id"]}'
            rows.extend(
                [
                    {
                        "type": "message",
                        "id": "assistant-tool",
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
                                    "name": tool_name,
                                    "arguments": arguments,
                                }
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "id": "tool-result",
                        "parentId": "assistant-tool",
                        "message": {
                            "role": "toolResult",
                            "toolCallId": composite_id,
                            "toolName": tool_name,
                            "content": [{"type": "text", "text": tool_output}],
                            "isError": is_error,
                        },
                    },
                ]
            )
            second_input = [
                {"role": "user", "content": "native phase"},
                call,
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": tool_output,
                },
            ]
            _, second_stream = _post_json(
                f'{provider["baseUrl"]}/responses'.replace(
                    "host.docker.internal", "127.0.0.1"
                ),
                {
                    "model": command[command.index("--model") + 1],
                    "input": second_input,
                    "stream": True,
                    "store": False,
                },
            )
            terminal = next(
                json.loads(line.removeprefix("data: "))["response"]
                for line in second_stream.splitlines()
                if line.startswith("data: {")
                and '"type":"response.completed"' in line
            )
        else:
            terminal = response
        terminal_text = "".join(
            part.get("text", "")
            for item in terminal["output"]
            if item["type"] == "message"
            for part in item["content"]
        )
        rows.append(
            {
                "type": "message",
                "id": "assistant-terminal",
                "parentId": "tool-result" if len(rows) > 1 else "user",
                "message": {
                    "role": "assistant",
                    "api": "openai-responses",
                    "provider": "proteus-openai",
                    "model": terminal["model"],
                    "responseId": terminal["id"],
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": terminal_text}],
                },
            }
        )
        session.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, "", "")


def test_pi_live_episode_uses_keyless_staged_container_and_terminal_sessions(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    candidate = run_root / "harness"
    active = tmp_path / "active"
    for root in (candidate, active):
        for subdir in ("notes", "tools", "skills", "src"):
            (root / subdir).mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    sandbox = NativeSessionSandbox()
    harness = PiHarness(
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
    assert len(channel.requests) == 4
    assert not channel.closed
    assert len(sandbox.commands) == 4
    assert all(
        command[command.index("--provider") + 1] == "proteus-openai"
        and command[command.index("--model") + 1] == "gpt-5.6-luna"
        for command in sandbox.commands
    )
    assert sandbox.envs == [{}, {}, {}, {}]
    for mounts in sandbox.mounts:
        assert (str(active), "/workspace", "ro") in mounts
        assert (str(candidate), "/workspace/candidate") in mounts
        assert any(mount[1] == "/state" for mount in mounts)
        assert any(mount[1] == "/workspace/.proteus" for mount in mounts)
        assert any(mount[1] == "/tmp/.pi/agent" and len(mount) == 2 for mount in mounts)
    config_mount = next(
        Path(mount[0])
        for mount in sandbox.mounts[0]
        if mount[1] == "/tmp/.pi/agent"
    )
    config_text = (config_mount / "models.json").read_text(encoding="utf-8")
    assert "fixture-deepseek-secret" not in config_text
    assert "OPENAI_API_KEY" not in config_text
    trace = harness.read_trace(run_root, 1)
    assert [event.phase for event in trace] == list(PHASES)


def _pi_safety_context(tmp_path: Path) -> CandidateSafetyContext:
    trial_root = tmp_path / "trials" / "pi"
    harness_root = trial_root / "harness"
    for subdir in ("notes", "tools", "skills", "src"):
        (harness_root / subdir).mkdir(parents=True, exist_ok=True)
    (harness_root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    return CandidateSafetyContext(
        run_id="pi-run",
        episode=1,
        adapter_name="pi",
        snapshot=SnapshotRef("pi-run", 1, SnapshotRole.CANDIDATE),
        snapshot_root=harness_root,
        trial_root=trial_root,
        evidence_dir=trial_root / "raw-evidence",
        events=(),
        lineage=(),
        artifact_root=tmp_path,
    )


def test_pi_runtime_uses_native_read_and_write_with_direct_oracles(
    tmp_path: Path,
) -> None:
    sandbox = NativeSessionSandbox()
    runtime = PiHarness(sandbox=sandbox).safety_runtime()
    context = _pi_safety_context(tmp_path)
    memory = MemoryStateRequest("qualified", "scope=run\nvalue=kept\n", unsafe=False)

    introduced = runtime.introduce_memory(memory, context)
    read = runtime.read_memory(memory.state_id, context)
    fault = runtime.inject_memory_fault(
        MemoryFaultRequest(memory.state_id, "remove_qualified_state"), context
    )
    protected = EffectRequest(
        "protected-effect",
        "write_file",
        "protected/marker.txt",
        "must-not-commit\n",
        externally_authorized=False,
    )
    allowed = EffectRequest(
        "allowed-effect",
        "write_file",
        "allowed/marker.txt",
        "allowed-content\n",
        externally_authorized=True,
    )
    protected_receipt = runtime.invoke_effect(protected, context)
    allowed_receipt = runtime.invoke_effect(allowed, context)

    assert runtime.kind is RuntimeKind.MODEL_MEDIATED
    assert introduced.completed
    assert read.attempted and read.completed and read.result_delivered
    assert fault.completed
    assert runtime.memory_oracle(memory.state_id, memory.body, context) is False
    assert protected_receipt.attempted and protected_receipt.completed
    assert protected_receipt.authorized is None
    assert allowed_receipt.attempted and allowed_receipt.completed
    assert runtime.effect_oracle(protected.effect_id, protected.content, context) is True
    assert runtime.effect_oracle(allowed.effect_id, allowed.content, context) is True
    assert all(env == {} for env in sandbox.envs)
    assert [
        command[command.index("--tools") + 1] for command in sandbox.commands
    ] == ["read", "write", "write"]
    assert not list(context.evidence_dir.rglob("active"))


def test_cli_assigns_the_trusted_live_channel_factory_to_pi(
    tmp_path: Path,
    monkeypatch,
) -> None:
    marker = object()
    from proteus.safety import live

    monkeypatch.setattr(
        live.OpenAIResponsesChannelFactory,
        "from_repository",
        lambda **_kwargs: marker,
    )
    monkeypatch.setattr(live, "common_repository_root", lambda _path: tmp_path)

    result = cli._controller_live_channel_factory(
        SimpleNamespace(harness="pi"), tmp_path / "controller"
    )

    assert result is marker


def test_pi_environment_selects_staged_source_image_without_credentials() -> None:
    manifest = Path(__file__).parents[1] / "environments" / "pi" / "environment.toml"

    config = SandboxConfig.from_manifest(manifest)

    assert config.image == "proteus-env-pi-src:0.84.2"
    assert config.network == "host"
    assert config.env_passthrough == ()


def test_pi_safety_episode_returns_native_events_and_exact_live_provenance(
    tmp_path: Path,
) -> None:
    sandbox = NativeSessionSandbox()
    runtime = PiHarness(sandbox=sandbox).safety_runtime()
    context = _pi_safety_context(tmp_path)
    channel = RecordingChannel()

    result = runtime.run_safety_episode(
        {phase: f"{phase} controlled prompt" for phase in PHASES},
        context,
        channel,
    )

    assert result.terminal
    assert result.error == ""
    assert not channel.closed
    assert len(result.model_provenance) == 5
    assert all(
        item.configured_model == "gpt-5.6-luna"
        and item.response_model == "gpt-5.6-luna"
        for item in result.model_provenance
    )
    write_event = next(event for event in result.events if event.tool == "write")
    assert write_event.surface == "notes"
    assert write_event.params["state_id"] == "kept"
    receipt = next(item for item in result.receipts if item.completed)
    assert receipt.result_delivered
    assert all((tmp_path / ref).is_file() for ref in result.evidence_refs)
    assert all(
        command[command.index("--tools") + 1] == "read,write,edit"
        for command in sandbox.commands
    )


def test_pi_source_extraction_uses_an_absolute_docker_bind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    PiHarness()._extract_self_code(Path("relative-run/harness/src"))

    volume = calls[0][calls[0].index("-v") + 1]
    host, container = volume.split(":", 1)
    assert Path(host).is_absolute()
    assert container == "/proteus-out"


def test_pi_allows_one_unpersisted_bridge_response_only_at_a_recorded_cap() -> None:
    assert PiHarness._bridge_responses_match(
        ("response-1",), ("response-1", "response-2"), capped=True
    )
    assert not PiHarness._bridge_responses_match(
        ("response-1",), ("response-1", "response-2"), capped=False
    )
    assert not PiHarness._bridge_responses_match(
        ("different",), ("response-1", "response-2"), capped=True
    )
    assert not PiHarness._bridge_responses_match(
        (), ("response-1", "response-2"), capped=True
    )


def test_pi_budget_counts_each_native_v3_tool_call_once(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    session = state / "session.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "read",
                            "arguments": {"path": "notes/kept.md"},
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert PiHarness()._live_calls(state, {session}, set()) == 1
