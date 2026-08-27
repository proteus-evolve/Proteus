from __future__ import annotations

import multiprocessing
from collections.abc import Mapping
from pathlib import Path
from types import FunctionType, MethodType, SimpleNamespace

import pytest
from safety_memory_fixtures import plant_harness_memory

from proteus import cli
from proteus.adapters.llm import LLMHarness, _render_state
from proteus.core.activation import CandidateGateContext
from proteus.core.disposition import NEUTRAL
from proteus.core.episode import RunConfig, run
from proteus.core.goal import GoalConfig
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.live import (
    LiveCallProvenance,
    LiveModelRequestOptions,
    LiveModelResponse,
    LiveModelUsage,
    LiveProtocolError,
    LiveToolCall,
    OpenAIResponsesChannelFactory,
)
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import RuntimeKind


def _nonreturning_responses_transport(_url, _payload, _headers, _timeout):
    while True:
        __import__("time").sleep(1)


class FakeChannel:
    model = "gpt-5.6-luna"

    def __init__(self) -> None:
        self.calls = 0
        self.inputs: list[object] = []
        self.closed = False

    def respond(self, *, input, instructions="", tools=()):
        del instructions, tools
        self.calls += 1
        self.inputs.append(input)
        output = (
            '[{"tool":"write_note","name":"phase1-qualified-memory",'
            '"text":"scope=run\\nqualification=controller-owned\\nvalue=preserve\\n"}]'
            if self.calls == 1
            else "[]"
        )
        provenance = LiveCallProvenance(
            call_id=f"call-{self.calls}",
            response_id=f"response-{self.calls}",
            configured_model=self.model,
            response_model=self.model,
        )
        return LiveModelResponse(
            response_id=provenance.response_id,
            model=self.model,
            output_text=output,
            tool_calls=(),
            provenance=provenance,
        )

    def close(self) -> None:
        self.closed = True


def _safety_context(tmp_path: Path) -> CandidateSafetyContext:
    trial_root = tmp_path / "trial"
    LLMHarness().seed(trial_root / "harness")
    return CandidateSafetyContext(
        run_id="llm-run",
        episode=1,
        adapter_name="llm",
        snapshot=SnapshotRef("llm-run", 1, SnapshotRole.CANDIDATE),
        snapshot_root=trial_root / "harness",
        trial_root=trial_root,
        evidence_dir=tmp_path / "evidence",
        events=(),
        lineage=(),
    )


def _contains(value: object, expected: object, seen: set[int]) -> bool:
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    if value == expected:
        return True
    if isinstance(value, dict):
        return any(_contains(item, expected, seen) for item in value.values())
    if isinstance(value, (tuple, list, set)):
        return any(_contains(item, expected, seen) for item in value)
    if isinstance(value, MethodType):
        return _contains(value.__self__, expected, seen)
    if isinstance(value, FunctionType):
        closure = value.__closure__ or ()
        return any(_contains(cell.cell_contents, expected, seen) for cell in closure)
    attributes = getattr(value, "__dict__", None)
    return isinstance(attributes, dict) and _contains(attributes, expected, seen)


def _run_config(
    tmp_path: Path,
    *,
    adapter: LLMHarness,
    channel_factory,
) -> RunConfig:
    return RunConfig(
        name="llm-test",
        adapter=adapter,
        disposition=NEUTRAL,
        goal=GoalConfig.no_goal(),
        root=tmp_path / "run",
        model="gpt-5.6-luna",
        episodes=1,
        max_turns=20,
        live_channel_factory=channel_factory,
    )


def test_llm_safety_runtime_executes_native_action_with_live_provenance(
    tmp_path: Path,
) -> None:
    context = _safety_context(tmp_path)
    channel = FakeChannel()
    runtime = LLMHarness().safety_runtime()

    result = runtime.run_safety_episode(
        {
            "observe": "inspect",
            "propose": "plan",
            "act": "act",
            "reflect": "reflect",
        },
        context,
        channel,
    )

    expected_body = "scope=run\nqualification=controller-owned\nvalue=preserve\n"
    assert runtime.kind is RuntimeKind.MODEL_MEDIATED
    assert (
        context.snapshot_root / "notes" / "phase1-qualified-memory.md"
    ).read_text() == expected_body
    assert result.terminal
    assert result.model_provenance == tuple(
        LiveCallProvenance(
            call_id=f"call-{number}",
            response_id=f"response-{number}",
            configured_model="gpt-5.6-luna",
            response_model="gpt-5.6-luna",
        )
        for number in range(1, 5)
    )
    action = next(event for event in result.events if event.tool == "write_note")
    assert action.params == {"state_id": "phase1-qualified-memory"}
    assert all("Current harness state:" in str(value) for value in channel.inputs)


def test_llm_safety_runtime_rejects_returned_model_mismatch(tmp_path: Path) -> None:
    class MismatchedChannel(FakeChannel):
        def respond(self, *, input, instructions="", tools=()):
            response = super().respond(
                input=input, instructions=instructions, tools=tools
            )
            provenance = LiveCallProvenance(
                call_id=response.provenance.call_id,
                response_id=response.response_id,
                configured_model=self.model,
                response_model="different-model",
            )
            return LiveModelResponse(
                response_id=response.response_id,
                model="different-model",
                output_text=response.output_text,
                tool_calls=response.tool_calls,
                provenance=provenance,
            )

    result = LLMHarness().safety_runtime().run_safety_episode(
        {
            "observe": "inspect",
            "propose": "plan",
            "act": "act",
            "reflect": "reflect",
        },
        _safety_context(tmp_path),
        MismatchedChannel(),
    )

    assert not result.terminal
    assert "provenance does not match" in result.error


def test_controller_opens_requested_model_channel_and_closes_it(
    tmp_path: Path,
) -> None:
    opened: list[tuple[str, str, FakeChannel]] = []

    def open_channel(model: str, cell_id: str) -> FakeChannel:
        channel = FakeChannel()
        opened.append((model, cell_id, channel))
        return channel

    result = run(
        _run_config(
            tmp_path,
            adapter=LLMHarness(),
            channel_factory=open_channel,
        )
    )

    assert result.error == ""
    assert [(model, cell_id) for model, cell_id, _ in opened] == [
        ("gpt-5.6-luna", "run.candidate.episode-001")
    ]
    assert opened[0][2].calls == 4
    assert opened[0][2].closed


def test_controller_closes_malformed_ordinary_channel_and_records_failure(
    tmp_path: Path,
) -> None:
    class MalformedClosableChannel:
        model = "gpt-5.6-luna"

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    channel = MalformedClosableChannel()

    result = run(
        _run_config(
            tmp_path,
            adapter=LLMHarness(),
            channel_factory=lambda _model, _cell_id: channel,
        )
    )

    assert "must implement LiveModelChannel" in result.error
    assert channel.closed


def test_controller_closes_ordinary_channel_when_adapter_setup_fails(
    tmp_path: Path,
) -> None:
    class SetupFailHarness(LLMHarness):
        def seed(self, harness_root: Path, rng_seed: int = 0) -> None:
            super().seed(harness_root, rng_seed)
            (harness_root / "notes").rmdir()
            (harness_root / "notes").write_text("blocks directory setup")

    channel = FakeChannel()

    result = run(
        _run_config(
            tmp_path,
            adapter=SetupFailHarness(),
            channel_factory=lambda _model, _cell_id: channel,
        )
    )

    assert result.error
    assert channel.closed


def test_responses_factory_routes_luna_and_keeps_key_out_of_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY='fixture-secret'\n")
    payloads: list[tuple[str, Mapping[str, object], int]] = []
    headers: list[Mapping[str, str]] = []

    def transport(url, payload, request_headers, timeout):
        payloads.append((url, payload, timeout))
        headers.append(request_headers)
        return {
            "id": "resp-1",
            "status": "completed",
            "model": "gpt-5.6-luna",
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "function_call",
                    "call_id": "call-exact-1",
                    "name": "write_note",
                    "arguments": '{"name":"kept","text":"body"}',
                },
            ],
            "usage": {"input_tokens": 17, "output_tokens": 9},
        }

    factory = OpenAIResponsesChannelFactory.from_repository(
        repository_root=tmp_path,
        evidence_root=tmp_path / "controller-ledgers",
        transport=transport,
    )
    channel = factory("gpt-5.6-luna", "memory.real_episode.candidate")

    response = channel.respond(
        input="phase prompt",
        instructions="native JSON actions only",
        tools=(
            {
                "type": "function",
                "name": "write_note",
                "parameters": {"type": "object", "properties": {}},
            },
        ),
    )
    channel.close()

    assert payloads[0][0] == "https://api.openai.com/v1/responses"
    assert payloads[0][1]["model"] == "gpt-5.6-luna"
    assert payloads[0][1]["input"] == "phase prompt"
    assert payloads[0][1]["instructions"] == "native JSON actions only"
    assert headers == [
        {"Authorization": "Bearer fixture-secret", "Content-Type": "application/json"}
    ]
    assert response.tool_calls[0].call_id == "call-exact-1"
    assert response.tool_calls[0].arguments == {"name": "kept", "text": "body"}
    assert response.provenance.configured_model == "gpt-5.6-luna"
    assert response.provenance.response_model == "gpt-5.6-luna"
    assert channel.input_tokens == 17
    assert channel.output_tokens == 9
    ledger_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "controller-ledgers").rglob("*.json")
    )
    assert "fixture-secret" not in ledger_text
    with pytest.raises(RuntimeError, match="closed"):
        channel.respond(input="late")


def test_responses_bounded_call_terminates_nonreturning_transport(
    tmp_path: Path,
) -> None:
    """Mutation caught: returning while a timed-out model operation is still alive."""
    from proteus.safety.live import OpenAIResponsesChannel

    channel = OpenAIResponsesChannel(
        model="gpt-5.6-luna",
        api_key="fixture-secret",
        evidence_dir=tmp_path / "bounded-ledger",
        transport=_nonreturning_responses_transport,
    )
    started = __import__("time").monotonic()

    with pytest.raises(TimeoutError, match="timed out"):
        channel.respond_bounded(input="never returns", timeout_s=0.1)

    elapsed = __import__("time").monotonic() - started
    assert elapsed < 2
    assert not any(
        child.name.startswith("proteus-live-call-")
        for child in multiprocessing.active_children()
    )


def test_responses_channel_accepts_sparse_completed_output_text(
    tmp_path: Path,
) -> None:
    def transport(_url, _payload, _headers, _timeout):
        return {
            "id": "resp-sparse-24",
            "status": "completed",
            "model": "gpt-5.6-luna",
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "first"}],
                },
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                },
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "second"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call-exact-sparse",
                    "name": "write_note",
                    "arguments": '{"name":"kept","text":"body"}',
                },
            ],
            "usage": {"input_tokens": 24, "output_tokens": 11},
        }

    channel = OpenAIResponsesChannelFactory(
        api_key="fixture-secret",
        evidence_root=tmp_path / "controller-ledgers",
        transport=transport,
    )("gpt-5.6-luna", "sparse.completed.response")

    response = channel.respond(input="phase prompt")

    assert response.output_text == "firstsecond"
    assert response.tool_calls == (
        LiveToolCall(
            call_id="call-exact-sparse",
            name="write_note",
            arguments={"name": "kept", "text": "body"},
        ),
    )
    assert response.provenance == LiveCallProvenance(
        call_id="call-001",
        response_id="resp-sparse-24",
        configured_model="gpt-5.6-luna",
        response_model="gpt-5.6-luna",
    )


def test_responses_channel_applies_optional_generation_controls_and_returns_usage(
    tmp_path: Path,
) -> None:
    observed = []

    def transport(url, payload, headers, timeout):
        observed.append((url, payload, headers, timeout))
        return {
            "id": "resp-controlled",
            "status": "completed",
            "model": "gpt-5.6-luna",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "controlled"}],
                }
            ],
            "usage": {"input_tokens": 31, "output_tokens": 17},
        }

    channel = OpenAIResponsesChannelFactory(
        api_key="fixture-secret",
        evidence_root=tmp_path / "controller-ledgers",
        transport=transport,
    )("gpt-5.6-luna", "controlled.request")

    response = channel.respond(
        input="phase prompt",
        options=LiveModelRequestOptions(
            max_output_tokens=65_536,
            temperature=0.7,
            reasoning_effort="medium",
        ),
    )

    assert observed[0][1]["max_output_tokens"] == 65_536
    assert observed[0][1]["temperature"] == 0.7
    assert observed[0][1]["reasoning"] == {"effort": "medium"}
    assert response.usage == LiveModelUsage(input_tokens=31, output_tokens=17)


@pytest.mark.parametrize(
    "options",
    [
        {"max_output_tokens": 0},
        {"temperature": 2.1},
        {"reasoning_effort": "unsupported"},
    ],
)
def test_live_model_request_options_reject_unsupported_controls(options) -> None:
    with pytest.raises(ValueError, match="max output|temperature|reasoning effort"):
        LiveModelRequestOptions(**options)


@pytest.mark.parametrize("text", [None, 17])
def test_responses_channel_rejects_non_string_output_text(
    tmp_path: Path,
    text: object,
) -> None:
    channel = OpenAIResponsesChannelFactory(
        api_key="fixture-secret",
        evidence_root=tmp_path / "controller-ledgers",
        transport=lambda *_args: {
            "id": "resp-invalid-text",
            "status": "completed",
            "model": "gpt-5.6-luna",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )("gpt-5.6-luna", "invalid.output.text")

    with pytest.raises(LiveProtocolError, match="output text must be non-empty text"):
        channel.respond(input="phase prompt")


def test_responses_channel_accepts_completed_response_with_only_empty_text(
    tmp_path: Path,
) -> None:
    channel = OpenAIResponsesChannelFactory(
        api_key="fixture-secret",
        evidence_root=tmp_path / "controller-ledgers",
        transport=lambda *_args: {
            "id": "resp-empty-text",
            "status": "completed",
            "model": "gpt-5.6-luna",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                },
            ],
            "usage": {"input_tokens": 1, "output_tokens": 0},
        },
    )("gpt-5.6-luna", "empty.output.text")

    response = channel.respond(input="phase prompt")
    assert response.output_text == ""
    assert response.tool_calls == ()


def test_responses_channel_does_not_treat_empty_refusal_as_sparse_text(
    tmp_path: Path,
) -> None:
    channel = OpenAIResponsesChannelFactory(
        api_key="fixture-secret",
        evidence_root=tmp_path / "controller-ledgers",
        transport=lambda *_args: {
            "id": "resp-empty-refusal",
            "status": "completed",
            "model": "gpt-5.6-luna",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "usable"},
                        {"type": "refusal", "refusal": ""},
                    ],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )("gpt-5.6-luna", "empty.refusal")

    with pytest.raises(LiveProtocolError, match="refusal must be non-empty text"):
        channel.respond(input="phase prompt")


def test_cli_binds_controller_channels_to_every_model_mediated_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, str, FakeChannel]] = []

    def controller_factory(model: str, cell_id: str) -> FakeChannel:
        channel = FakeChannel()
        opened.append((model, cell_id, channel))
        return channel

    from proteus.safety import live

    monkeypatch.setattr(
        live.OpenAIResponsesChannelFactory,
        "from_repository",
        lambda **_kwargs: controller_factory,
    )
    monkeypatch.setattr(live, "common_repository_root", lambda _path: tmp_path)
    args = SimpleNamespace(
        harness="llm",
        env="",
        network="",
        mem="",
        cpus="",
        docker_arg=[],
        phase_timeout=0,
        out=str(tmp_path / "controller"),
        safety_suite="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
    )
    adapter_factory = cli._harness_factory(args)
    live_channel_factory = cli._controller_live_channel_factory(
        args, tmp_path / "controller"
    )
    gate = cli._candidate_gate_factory(
        args,
        adapter_factory=adapter_factory,
        controller_root=tmp_path / "controller",
        channel_factory=live_channel_factory,
    )("llm-run")
    active_root = tmp_path / "subject" / "active"
    candidate_root = tmp_path / "subject" / "candidate"
    LLMHarness().seed(active_root)
    LLMHarness().seed(candidate_root)
    plant_harness_memory(active_root)
    plant_harness_memory(candidate_root)

    gate.evaluate(
        CandidateGateContext(
            run_id="llm-run",
            episode=1,
            active=SnapshotRef("llm-run", 0, SnapshotRole.ACTIVE),
            candidate=SnapshotRef("llm-run", 1, SnapshotRole.CANDIDATE),
            active_root=active_root,
            candidate_root=candidate_root,
            events=(),
        )
    )

    permission_cells = [
        cell_id for _, cell_id, _ in opened if "tools_permission_drift" in cell_id
    ]
    assert len(opened) == 12
    assert {model for model, _, _ in opened} == {"gpt-5.6-luna"}
    assert all(cell_id.startswith("llm-run.episode-") for _, cell_id, _ in opened)
    assert {cell.split(".")[-2] for cell in permission_cells} == {
        "protected_overwrite",
        "policy_mutation",
        "tool_skill_capability_minting",
        "workspace_boundary",
    }
    assert all(channel.closed for _, _, channel in opened)


def test_cli_llm_adapter_does_not_retain_controller_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CredentialOwningFactory:
        def __init__(self) -> None:
            self.api_key = "fixture-secret"

        def __call__(self, model: str, cell_id: str) -> FakeChannel:
            del model, cell_id
            return FakeChannel()

    from proteus.safety import live

    monkeypatch.setattr(
        live.OpenAIResponsesChannelFactory,
        "from_repository",
        lambda **_kwargs: CredentialOwningFactory(),
    )
    monkeypatch.setattr(live, "common_repository_root", lambda _path: tmp_path)
    args = SimpleNamespace(
        harness="llm",
        env="",
        network="",
        mem="",
        cpus="",
        docker_arg=[],
        phase_timeout=0,
        out=str(tmp_path / "controller"),
    )

    controller_factory = cli._controller_live_channel_factory(
        args, tmp_path / "controller"
    )
    adapter = cli._harness_factory(args)()

    assert _contains(controller_factory, "fixture-secret", set())
    assert not _contains(adapter, "fixture-secret", set())


class FakeResponsesTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def __call__(self, url, payload, headers, timeout):
        del url, headers, timeout
        self.calls += 1
        if self.fail:
            raise RuntimeError("fixture transport failure")
        return {
            "id": f"response-{self.calls}",
            "status": "completed",
            "model": payload["model"],
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "[]"}],
                }
            ],
            "usage": {"input_tokens": 7, "output_tokens": 1},
        }


def _llm_cli_args(output: Path) -> list[str]:
    return [
        "run",
        "--harness",
        "llm",
        "--arm",
        "neutral",
        "--seeds",
        "1",
        "--episodes",
        "1",
        "--model",
        "gpt-5.6-luna",
        "--max-turns",
        "20",
        "--out",
        str(output),
    ]


def test_llm_resume_preserves_failed_ledger_and_allocates_new_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "resume-output"
    transports = [FakeResponsesTransport(fail=True), FakeResponsesTransport()]

    def from_repository(**kwargs):
        return OpenAIResponsesChannelFactory(
            api_key="fixture-secret",
            evidence_root=kwargs["evidence_root"],
            transport=transports.pop(0),
        )

    from proteus.safety import live

    monkeypatch.setattr(
        live.OpenAIResponsesChannelFactory,
        "from_repository",
        from_repository,
    )
    monkeypatch.setattr(live, "common_repository_root", lambda _path: tmp_path)

    assert cli.main(_llm_cli_args(output)) == 1
    assert cli.main(_llm_cli_args(output) + ["--on-existing", "resume"]) == 0

    cell_root = next((output / "live-model-ledgers").iterdir())
    assert sorted(path.name for path in cell_root.iterdir()) == [
        "attempt-000001",
        "attempt-000002",
    ]
    assert (cell_root / "attempt-000001" / "request-001.json").is_file()
    assert (cell_root / "attempt-000002" / "response-004.json").is_file()


def test_llm_overwrite_removes_prior_live_ledgers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "overwrite-output"
    transports = [FakeResponsesTransport(), FakeResponsesTransport()]

    def from_repository(**kwargs):
        return OpenAIResponsesChannelFactory(
            api_key="fixture-secret",
            evidence_root=kwargs["evidence_root"],
            transport=transports.pop(0),
        )

    from proteus.safety import live

    monkeypatch.setattr(
        live.OpenAIResponsesChannelFactory,
        "from_repository",
        from_repository,
    )
    monkeypatch.setattr(live, "common_repository_root", lambda _path: tmp_path)

    assert cli.main(_llm_cli_args(output)) == 0
    stale = output / "live-model-ledgers" / "stale-controller-artifact"
    stale.mkdir()
    assert cli.main(_llm_cli_args(output) + ["--on-existing", "overwrite"]) == 0

    assert not stale.exists()
    cell_root = next((output / "live-model-ledgers").iterdir())
    assert sorted(path.name for path in cell_root.iterdir()) == ["attempt-000001"]


def test_llm_safety_runtime_does_not_retain_controller_credential(
    tmp_path: Path,
) -> None:
    controller_factory = OpenAIResponsesChannelFactory(
        api_key="fixture-secret",
        evidence_root=tmp_path / "ledgers",
        transport=lambda *_args: {},
    )

    runtime = LLMHarness().safety_runtime()

    assert not _contains(runtime, "fixture-secret", set())
    assert not _contains(runtime, controller_factory, set())


def test_llm_state_rendering_handles_native_effect_directories(tmp_path: Path) -> None:
    harness_root = tmp_path / "harness"
    LLMHarness().seed(harness_root)
    marker = harness_root / "tools" / "allowed" / "marker.txt"
    marker.parent.mkdir()
    marker.write_text("allowed-effect-committed\n")

    rendered = _render_state(harness_root)

    assert "allowed/" in rendered
