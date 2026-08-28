from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from proteus import cli
from proteus.adapters.pi import PiHarness, PiNativeEpisode, PiSessionEvidence
from proteus.adapters.pi_safety import PiSafetyRuntime
from proteus.core.adapter import EpisodeResult, EpisodeSpec
from proteus.core.budget import PHASES
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import ProbeEndpoint
from proteus.safety.live import (
    LiveCallProvenance,
    LiveModelResponse,
    LiveToolCall,
)
from proteus.safety.live_bridge import OpenAICompatibleBridge
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import (
    MemoryFaultRequest,
    MemoryStateRequest,
    RuntimeKind,
)
from proteus.sandbox import SandboxConfig


def test_pi_safety_runtime_rejects_non_channel_as_type_error() -> None:
    runtime = PiSafetyRuntime(PiHarness())

    with pytest.raises(TypeError, match="requires a live model channel"):
        runtime.run_safety_episode({}, None, object())  # type: ignore[arg-type]


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
        snapshot=SnapshotRef("pi-run", 1, SnapshotRole.ACTIVE),
        snapshot_root=harness_root,
        trial_root=trial_root,
        evidence_dir=trial_root / "raw-evidence",
        endpoint=ProbeEndpoint.SETTLED,
        events=(),
        lineage=(),
        artifact_root=tmp_path,
    )


def test_pi_runtime_uses_native_memory_read_with_direct_oracle(
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
    assert runtime.kind is RuntimeKind.MODEL_MEDIATED
    assert introduced.completed
    assert read.attempted and read.completed and read.result_delivered
    assert fault.completed
    assert runtime.memory_oracle(memory.state_id, memory.body, context) is False
    assert all(env == {} for env in sandbox.envs)
    assert [
        command[command.index("--tools") + 1] for command in sandbox.commands
    ] == ["read"]
    assert not list(context.evidence_dir.rglob("active"))


def test_cli_preserves_default_pi_routing_without_opening_controller(
    tmp_path: Path,
    monkeypatch,
) -> None:
    opened = 0
    marker = object()
    from proteus.safety import live

    def from_repository(**_kwargs):
        nonlocal opened
        opened += 1
        return marker

    monkeypatch.setattr(
        live.OpenAIResponsesChannelFactory,
        "from_repository",
        from_repository,
    )
    monkeypatch.setattr(live, "common_repository_root", lambda _path: tmp_path)

    result = cli._controller_live_channel_factory(
        SimpleNamespace(harness="pi", model="", safety_suite=""),
        tmp_path / "controller",
    )

    assert result is None
    assert opened == 0


def test_cli_routes_explicit_safety_luna_through_controller(
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
    args = SimpleNamespace(
        harness="pi",
        model="gpt-5.6-luna",
        safety_suite="proteus.safety.phase1:SUITE",
    )

    controller = cli._controller_live_channel_factory(args, tmp_path / "controller")

    assert controller is marker
    assert cli._ordinary_live_channel_factory(args, controller) is marker


def test_pi_safety_factory_does_not_replace_empty_ordinary_model(
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
    args = SimpleNamespace(
        harness="pi",
        model="",
        safety_suite="proteus.safety.phase1:SUITE",
    )

    controller = cli._controller_live_channel_factory(args, tmp_path / "controller")

    assert controller is marker
    assert cli._ordinary_live_channel_factory(args, controller) is None


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


def test_pi_safety_episode_is_not_terminal_when_later_phases_are_budget_skipped(
    tmp_path: Path,
) -> None:
    class BudgetCappedPiHarness(PiHarness):
        def run_live_episode(self, spec, *, evidence_root, enabled_tools=()):
            del spec, evidence_root, enabled_tools
            session_path = tmp_path / "trials" / "pi" / ".pi-state" / "observe.jsonl"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text("", encoding="utf-8")
            return PiNativeEpisode(
                result=EpisodeResult(
                    episode=1,
                    ok=True,
                    turns=20,
                    counters={"phases": 1, "turn_capped": True},
                ),
                sessions=(PiSessionEvidence(True, (), (), (), (), ()),),
                session_paths=(session_path,),
                bridge_records=(),
                bridge_root=None,
            )

    runtime = BudgetCappedPiHarness().safety_runtime()

    result = runtime.run_safety_episode(
        {phase: f"{phase} controlled prompt" for phase in PHASES},
        _pi_safety_context(tmp_path),
        TextChannel(),
    )

    assert result.terminal is False
    assert "required native Pi phases did not complete" in result.error


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
    assert PiHarness._bridge_responses_match(
        ("r1", "r2", "r3", "r4"),
        ("r1", "r2", "extra-1", "r3", "r4", "extra-2"),
        capped=True,
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


def test_bridge_rejects_unknown_and_duplicate_tool_results_before_forwarding(
    tmp_path: Path,
) -> None:
    channel = RecordingChannel()
    with OpenAICompatibleBridge(
        channel=channel,
        evidence_root=tmp_path / "bridge",
    ) as bridge:
        _post_json(
            f"{bridge.host_base_url}/responses",
            {"model": channel.model, "input": "issue a call", "stream": True},
        )
        unknown = [
            {
                "type": "function_call_output",
                "call_id": "call-never-issued",
                "output": "invented",
            }
        ]
        with pytest.raises(urllib.error.HTTPError) as unknown_error:
            _post_json(
                f"{bridge.host_base_url}/responses",
                {"model": channel.model, "input": unknown, "stream": True},
            )
        duplicate = [
            {
                "type": "function_call_output",
                "call_id": "call-native-write",
                "output": "first",
            },
            {
                "type": "function_call_output",
                "call_id": "call-native-write",
                "output": "second",
            },
        ]
        with pytest.raises(urllib.error.HTTPError) as duplicate_error:
            _post_json(
                f"{bridge.host_base_url}/responses",
                {"model": channel.model, "input": duplicate, "stream": True},
            )

    assert unknown_error.value.code == 400
    assert duplicate_error.value.code == 400
    assert len(channel.requests) == 1


def test_pi_rejects_session_calls_without_controller_ownership() -> None:
    assert not PiHarness._bridge_tool_calls_match(
        ("call-self-consistent|fc-session-1",),
        ("call-self-consistent|fc-session-1",),
        ("call-issued-by-controller",),
        capped=False,
    )
def test_pi_session_requires_boolean_tool_result_error(tmp_path: Path) -> None:
    session = tmp_path / "malformed-result.jsonl"
    rows = [
        {"type": "session", "version": 3, "id": "session-malformed"},
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "api": "openai-responses",
                "provider": "proteus-openai",
                "model": "gpt-5.6-luna",
                "responseId": "response-tool",
                "stopReason": "toolUse",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call-1|fc-1",
                        "name": "read",
                        "arguments": {"path": "notes/kept.md"},
                    }
                ],
            },
        },
        {
            "type": "message",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-1|fc-1",
                "toolName": "read",
                "content": [{"type": "text", "text": "kept"}],
            },
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "api": "openai-responses",
                "provider": "proteus-openai",
                "model": "gpt-5.6-luna",
                "responseId": "response-terminal",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "done"}],
            },
        },
    ]
    session.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    evidence = PiHarness()._session_evidence(
        session,
        phase="act",
        expected_provider="proteus-openai",
        expected_model="gpt-5.6-luna",
        evidence_ref="malformed-result.jsonl",
    )

    assert not evidence.terminal
    assert "isError must be boolean" in evidence.error
    assert not evidence.receipts[0].completed


def test_pi_context_preparation_uses_checked_in_catalog_bundle(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[1]
    context = tmp_path / "pi-context"
    data = context / "packages" / "ai" / "src" / "providers" / "data"
    data.mkdir(parents=True)
    (context / "packages" / "ai" / "package.json").write_text(
        '{"name":"@earendil-works/pi-ai"}\n', encoding="utf-8"
    )
    (data / "stale.json").write_text("{}\n", encoding="utf-8")

    result = subprocess.run(
        [str(repository / "environments" / "pi-src" / "prepare-context.sh"), str(context)],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (data / "stale.json").exists()
    manifest = json.loads((data / ".manifest.json").read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 3
    assert "cloudflare-ai-gateway.json" in manifest["files"]
    assert (context / ".proteus-boot.sh").read_bytes() == (
        repository / "environments" / "pi-src" / "boot.sh"
    ).read_bytes()
