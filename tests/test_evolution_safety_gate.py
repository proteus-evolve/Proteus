from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from proteus import cli
from proteus.adapters.minimal import MinimalHarness
from proteus.adapters.minimal_safety import MinimalSafetyRuntime
from proteus.core.activation import CandidateGateContext
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import EvidenceCellObservation
from proteus.safety.gate import build_candidate_gate_factory
from proteus.safety.live import LiveCallProvenance
from proteus.safety.phase1 import SUITE
from proteus.safety.runtime import RuntimeKind
from proteus.safety.taxonomy import EvidenceStratum, SafetyStatus


def _gate_context(tmp_path: Path) -> CandidateGateContext:
    active_root = tmp_path / "subject" / "active"
    candidate_root = tmp_path / "subject" / "candidate"
    MinimalHarness().seed(active_root)
    MinimalHarness().seed(candidate_root)
    return CandidateGateContext(
        run_id="matched-run",
        episode=1,
        active=SnapshotRef("matched-run", 0, SnapshotRole.ACTIVE),
        candidate=SnapshotRef("matched-run", 1, SnapshotRole.CANDIDATE),
        active_root=active_root,
        candidate_root=candidate_root,
        events=(),
    )


def test_gate_administers_each_family_once_per_endpoint_and_projects_declared_cells(
    tmp_path: Path,
) -> None:
    controller_root = tmp_path / "controller"
    gate = build_candidate_gate_factory(
        adapter_factory=MinimalHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="",
        controller_root=controller_root,
    )("matched-run")

    decision = gate.evaluate(_gate_context(tmp_path))

    assert not decision.allowed
    gate_root = (controller_root / decision.decision_ref).parent
    published = [json.loads(line) for line in (gate_root / "results.jsonl").read_text().splitlines()]
    assert len(published) == len(SUITE.definitions()) * 2
    by_family_endpoint = {
        (item["family_id"], item["endpoint"]): item for item in published
    }
    assert set(by_family_endpoint) == {
        (definition.family_id, endpoint)
        for definition in SUITE.definitions()
        for endpoint in ("active", "candidate")
    }
    for definition in SUITE.definitions():
        declared = [cell.cell_id for cell in definition.declared_cells]
        for endpoint in ("active", "candidate"):
            observation = by_family_endpoint[(definition.family_id, endpoint)]
            assert [cell["cell_id"] for cell in observation["cells"]] == declared
            for cell in observation["cells"]:
                if cell["status"] in {SafetyStatus.PASS.value, SafetyStatus.FAIL.value}:
                    assert cell["evidence_refs"]
                    assert all(
                        f"/{cell['cell_id']}/" in f"/{ref}"
                        for ref in cell["evidence_refs"]
                    )
                    assert all((gate_root / ref).is_file() for ref in cell["evidence_refs"])

    candidate_outcomes = {
        family_id: item["status"]
        for (family_id, endpoint), item in by_family_endpoint.items()
        if endpoint == "candidate"
    }
    assert candidate_outcomes == {
        "memory_bad_admission": "fail",
        "memory_collapse": "fail",
        "tools_permission_drift": "pass",
    }
    tools = by_family_endpoint[("tools_permission_drift", "candidate")]
    real_episode = next(
        cell for cell in tools["cells"]
        if cell["stratum"] == EvidenceStratum.REAL_EPISODE.value
    )
    assert real_episode["status"] == SafetyStatus.NOT_EVALUATED.value
    assert real_episode["reason"] == "no_exact_proposal"
    assert tools["status"] == SafetyStatus.PASS.value


def test_malformed_selected_runtime_uses_clean_cli_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MalformedHarness(MinimalHarness):
        name = "malformed"

        def safety_runtime(self):
            return object()

    monkeypatch.setattr(cli, "_harness_factory", lambda _args: MalformedHarness)
    output_root = tmp_path / "must-not-exist"

    with pytest.raises(SystemExit, match="does not implement HarnessSafetyRuntime"):
        cli.main(
            [
                "run",
                "--harness",
                "minimal",
                "--arm",
                "neutral",
                "--seeds",
                "1",
                "--episodes",
                "1",
                "--safety-suite",
                "proteus.safety.phase1:SUITE",
                "--out",
                str(output_root),
            ]
        )

    assert not output_root.exists()


def test_evidence_cell_rejects_malformed_administration_fields() -> None:
    with pytest.raises(TypeError, match="administered and oracle_complete must be booleans"):
        EvidenceCellObservation(
            cell_id="family.native_boundary",
            stratum=EvidenceStratum.NATIVE_BOUNDARY,
            status=SafetyStatus.NOT_EVALUATED,
            administered="yes",  # type: ignore[arg-type]
            oracle_complete=False,
            violation=None,
            evidence_refs=(),
        )


def test_terminal_evidence_cell_requires_an_observed_violation_value() -> None:
    with pytest.raises(ValueError, match="terminal evidence cell requires a violation"):
        EvidenceCellObservation(
            cell_id="family.native_boundary",
            stratum=EvidenceStratum.NATIVE_BOUNDARY,
            status=SafetyStatus.PASS,
            administered=True,
            oracle_complete=True,
            violation=None,
            evidence_refs=("evidence/family.json",),
        )


def test_model_runtime_gets_one_closed_channel_per_real_episode_cell(
    tmp_path: Path,
) -> None:
    class ModelRuntime(MinimalSafetyRuntime):
        kind = RuntimeKind.MODEL_MEDIATED

        def run_safety_episode(self, prompts, context, channel):
            assert channel is not None
            result = super().run_safety_episode(prompts, context, None)
            provenance = LiveCallProvenance(
                call_id=f"call-{context.snapshot.role.value}",
                response_id=f"response-{context.snapshot.role.value}",
                configured_model="gpt-5.6-luna",
                response_model="gpt-5.6-luna",
            )
            return replace(result, model_provenance=(provenance,))

    class ModelHarness(MinimalHarness):
        name = "model-fixture"

        def safety_runtime(self):
            return ModelRuntime(self)

    class Channel:
        def __init__(self, cell_id: str) -> None:
            self.cell_id = cell_id
            self.closed = False

        @property
        def model(self) -> str:
            return "gpt-5.6-luna"

        def close(self) -> None:
            self.closed = True

        def respond(self, *, input, instructions="", tools=()):
            del input, instructions, tools
            raise AssertionError("fixture runtime owns the deterministic response")

    channels: list[Channel] = []

    def channel_factory(model: str, cell_id: str) -> Channel:
        assert model == "gpt-5.6-luna"
        channel = Channel(cell_id)
        channels.append(channel)
        return channel

    gate = build_candidate_gate_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        channel_factory=channel_factory,
    )("model-run")

    gate.evaluate(_gate_context(tmp_path))

    assert len(channels) == len(SUITE.definitions()) * 2
    assert all(".real_episode." in channel.cell_id for channel in channels)
    assert all(channel.closed for channel in channels)


def test_model_channel_without_close_is_rejected_before_use(tmp_path: Path) -> None:
    class ModelRuntime(MinimalSafetyRuntime):
        kind = RuntimeKind.MODEL_MEDIATED

    class ModelHarness(MinimalHarness):
        name = "model-fixture"

        def safety_runtime(self):
            return ModelRuntime(self)

    class NoCloseChannel:
        @property
        def model(self) -> str:
            return "gpt-5.6-luna"

        def respond(self, *, input, instructions="", tools=()):
            del input, instructions, tools
            raise AssertionError("malformed channel must be rejected before use")

    gate = build_candidate_gate_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        channel_factory=lambda _model, _cell_id: NoCloseChannel(),
    )("model-run")

    with pytest.raises(TypeError, match="must implement LiveModelChannel"):
        gate.evaluate(_gate_context(tmp_path))

    assert not (tmp_path / "controller" / "safety-gates" / "matched-run" / "episode-001").exists()


def test_malformed_closable_model_channel_is_closed_after_protocol_rejection(
    tmp_path: Path,
) -> None:
    class ModelRuntime(MinimalSafetyRuntime):
        kind = RuntimeKind.MODEL_MEDIATED

    class ModelHarness(MinimalHarness):
        name = "model-fixture"

        def safety_runtime(self):
            return ModelRuntime(self)

    class MalformedClosableChannel:
        closed = False

        @property
        def model(self) -> str:
            return "gpt-5.6-luna"

        def close(self) -> None:
            self.closed = True

    channel = MalformedClosableChannel()
    gate = build_candidate_gate_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        channel_factory=lambda _model, _cell_id: channel,
    )("model-run")

    with pytest.raises(TypeError, match="must implement LiveModelChannel"):
        gate.evaluate(_gate_context(tmp_path))

    assert channel.closed


def test_model_channel_closes_when_executor_raises(tmp_path: Path) -> None:
    class FailingModelRuntime(MinimalSafetyRuntime):
        kind = RuntimeKind.MODEL_MEDIATED

        def run_safety_episode(self, prompts, context, channel):
            del prompts, context, channel
            raise RuntimeError("executor failed")

    class ModelHarness(MinimalHarness):
        name = "model-fixture"

        def safety_runtime(self):
            return FailingModelRuntime(self)

    class Channel:
        closed = False

        @property
        def model(self) -> str:
            return "gpt-5.6-luna"

        def respond(self, *, input, instructions="", tools=()):
            del input, instructions, tools
            raise AssertionError("fixture runtime fails before a response")

        def close(self) -> None:
            self.closed = True

    channel = Channel()
    gate = build_candidate_gate_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        channel_factory=lambda _model, _cell_id: channel,
    )("model-run")

    with pytest.raises(RuntimeError, match="executor failed"):
        gate.evaluate(_gate_context(tmp_path))

    assert channel.closed


def test_model_channel_close_failure_cannot_publish_a_decision(tmp_path: Path) -> None:
    class ModelRuntime(MinimalSafetyRuntime):
        kind = RuntimeKind.MODEL_MEDIATED

        def run_safety_episode(self, prompts, context, channel):
            result = super().run_safety_episode(prompts, context, None)
            provenance = LiveCallProvenance(
                call_id="call-close-failure",
                response_id="response-close-failure",
                configured_model="gpt-5.6-luna",
                response_model="gpt-5.6-luna",
            )
            return replace(result, model_provenance=(provenance,))

    class ModelHarness(MinimalHarness):
        name = "model-fixture"

        def safety_runtime(self):
            return ModelRuntime(self)

    class CloseFailureChannel:
        @property
        def model(self) -> str:
            return "gpt-5.6-luna"

        def respond(self, *, input, instructions="", tools=()):
            del input, instructions, tools
            raise AssertionError("fixture runtime owns the deterministic response")

        def close(self) -> None:
            raise RuntimeError("channel close failed")

    gate = build_candidate_gate_factory(
        adapter_factory=ModelHarness,
        suite_spec="proteus.safety.phase1:SUITE",
        safety_model="gpt-5.6-luna",
        controller_root=tmp_path / "controller",
        channel_factory=lambda _model, _cell_id: CloseFailureChannel(),
    )("model-run")

    with pytest.raises(RuntimeError, match="channel close failed"):
        gate.evaluate(_gate_context(tmp_path))

    assert not (tmp_path / "controller" / "safety-gates" / "matched-run" / "episode-001").exists()
