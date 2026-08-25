from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from proteus import cli
from proteus.adapters.llm import LLMHarness, _render_state
from proteus.core.activation import CandidateGateContext
from proteus.core.adapter import EpisodeSpec
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.live import (
    LiveCallProvenance,
    LiveModelResponse,
    OpenAIResponsesChannelFactory,
)
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import RuntimeKind


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
    attributes = getattr(value, "__dict__", None)
    return isinstance(attributes, dict) and _contains(attributes, expected, seen)


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


def test_llm_candidate_uses_requested_model_channel_and_closes_it(
    tmp_path: Path,
) -> None:
    opened: list[tuple[str, str, FakeChannel]] = []

    def open_channel(model: str, cell_id: str) -> FakeChannel:
        channel = FakeChannel()
        opened.append((model, cell_id, channel))
        return channel

    harness = LLMHarness(channel_factory=open_channel)
    harness.seed(tmp_path / "harness")

    result = harness.run_episode(
        EpisodeSpec(
            root=tmp_path,
            episode=1,
            model="gpt-5.6-luna",
            phase_prompts={
                "observe": "inspect",
                "propose": "plan",
                "act": "act",
                "reflect": "reflect",
            },
            max_turns=20,
        )
    )

    assert result.ok
    assert [(model, cell_id) for model, cell_id, _ in opened] == [
        ("gpt-5.6-luna", "candidate.episode-001")
    ]
    assert opened[0][2].calls == 4
    assert opened[0][2].closed


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
    gate = cli._candidate_gate_factory(
        args,
        adapter_factory=adapter_factory,
        controller_root=tmp_path / "controller",
    )("llm-run")
    active_root = tmp_path / "subject" / "active"
    candidate_root = tmp_path / "subject" / "candidate"
    LLMHarness().seed(active_root)
    LLMHarness().seed(candidate_root)

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

    assert len(opened) == 6
    assert {model for model, _, _ in opened} == {"gpt-5.6-luna"}
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

    adapter = cli._harness_factory(args)()

    assert not _contains(adapter, "fixture-secret", set())


def test_llm_safety_runtime_does_not_retain_controller_credential(
    tmp_path: Path,
) -> None:
    controller_factory = OpenAIResponsesChannelFactory(
        api_key="fixture-secret",
        evidence_root=tmp_path / "ledgers",
        transport=lambda *_args: {},
    )

    runtime = LLMHarness(channel_factory=controller_factory).safety_runtime()

    assert not _contains(runtime, "fixture-secret", set())


def test_llm_state_rendering_handles_native_effect_directories(tmp_path: Path) -> None:
    harness_root = tmp_path / "harness"
    LLMHarness().seed(harness_root)
    marker = harness_root / "tools" / "allowed" / "marker.txt"
    marker.parent.mkdir()
    marker.write_text("allowed-effect-committed\n")

    rendered = _render_state(harness_root)

    assert "allowed/" in rendered
