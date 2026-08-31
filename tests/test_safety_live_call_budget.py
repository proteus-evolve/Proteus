from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from proteus import cli
from proteus.adapters.dsh import DshHarness
from proteus.adapters.minimal import MinimalHarness
from proteus.core.activation import SettledEpisodeSafetyContext
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.gate import PostEpisodeSafetyRunner
from proteus.safety.live import (
    ControllerLiveCallBudget,
    LiveCallBudgetPlan,
    LiveCallCategory,
    LiveProtocolError,
    OpenAIResponsesChannelFactory,
    derive_builtin_live_call_plan,
)
from proteus.safety.permission_adapter import UnsupportedPermissionPolicyAdapter
from proteus.safety.phase1 import TOOLS_PERMISSION_DRIFT
from proteus.safety.runtime import RuntimeKind


class CountingChannel:
    model = "counting-model"

    def __init__(self) -> None:
        self.calls = 0

    def respond(self, **_kwargs):
        self.calls += 1
        return object()

    def close(self) -> None:
        return None


def test_cli_dsh_permission_budget_matches_adapter_capabilities() -> None:
    adapter = DshHarness(key="", sandbox=object()).permission_policy_adapter()

    assert cli._builtin_permission_supported_cases("dsh") == len(
        adapter.declared_supported_case_ids
    ) == 5


@pytest.mark.parametrize(
    ("harness", "turns", "supported", "ordinary", "safety", "total"),
    [
        ("minimal", 20, 2, 0, 0, 0),
        ("llm", 20, 2, 4, 0, 4),
        ("pi", 8, 5, 12, 0, 12),
        ("dsh", 8, 5, 16, 0, 16),
        ("aki", 56, 5, 56, 0, 56),
    ],
)
def test_live_call_plan_derives_exact_whole_run_caps(
    harness, turns, supported, ordinary, safety, total
) -> None:
    plan = derive_builtin_live_call_plan(
        harness=harness,
        episodes=1,
        ordinary_hard_limit=turns,
        permission_supported_cases=supported,
    )
    assert (plan.ordinary_cap, plan.safety_cap, plan.total_cap) == (
        ordinary,
        safety,
        total,
    )


@pytest.mark.parametrize(
    ("harness", "turns", "supported", "ordinary", "safety", "total"),
    [
        ("minimal", 20, 2, 0, 0, 0),
        ("llm", 20, 2, 4, 32, 36),
        ("pi", 8, 5, 12, 64, 76),
        ("dsh", 8, 5, 16, 1, 17),
        ("aki", 56, 5, 56, 64, 120),
    ],
)
def test_live_call_plan_phase1_memory_families_use_actual_provider_calls(
    harness, turns, supported, ordinary, safety, total
) -> None:
    plan = derive_builtin_live_call_plan(
        harness=harness,
        episodes=1,
        ordinary_hard_limit=turns,
        permission_supported_cases=supported,
        include_memory_families=True,
    )
    assert (plan.ordinary_cap, plan.safety_cap, plan.total_cap) == (
        ordinary,
        safety,
        total,
    )


def test_live_call_plan_safety_caps_scale_with_scheduled_episodes() -> None:
    one = derive_builtin_live_call_plan(
        harness="dsh",
        episodes=1,
        ordinary_hard_limit=8,
        permission_supported_cases=5,
        include_memory_families=True,
    )
    twenty = derive_builtin_live_call_plan(
        harness="dsh",
        episodes=20,
        ordinary_hard_limit=8,
        permission_supported_cases=5,
        include_memory_families=True,
        collapse_episode_count=5,
    )
    assert one.safety_cap == 1
    assert twenty.safety_cap == 5
    assert twenty.ordinary_cap == 16 * 20
    assert twenty.total_cap == twenty.ordinary_cap + twenty.safety_cap


def test_controller_budget_stops_before_provider_call_and_never_retries(tmp_path: Path) -> None:
    provider = CountingChannel()
    budget = ControllerLiveCallBudget(
        LiveCallBudgetPlan("dsh", ordinary_cap=1, safety_cap=2),
        tmp_path / "call-budget.json",
    )
    channel = budget.wrap(
        provider,
        category=LiveCallCategory.SAFETY,
        cell_id="case.active",
        channel_cap=2,
    )
    channel.respond(input="first")
    channel.respond(input="delivery")
    with pytest.raises(LiveProtocolError, match="safety live-call cap exhausted"):
        channel.respond(input="retry")
    assert provider.calls == 2
    assert budget.snapshot()["actual"] == {"ordinary": 0, "safety": 2, "total": 2}


class PreflightRecordingAdapter(UnsupportedPermissionPolicyAdapter):
    def __init__(self, events: list[str], tmp_path: Path) -> None:
        super().__init__(
            name="preflight",
            kind=RuntimeKind.DETERMINISTIC,
            missing_requirement="native_authorization_decision_unavailable",
        )
        self.events = events
        self.tmp_path = tmp_path

    def capability(self, case_spec, snapshot_context):
        self.events.append(
            f"capability:{snapshot_context.snapshot.role.value}:{case_spec.case_id}"
        )
        preflight = self.tmp_path / "controller/preflight/tools_permission_drift.json"
        if preflight.is_file() and "preflight_written" not in self.events:
            self.events.append("preflight_written")
        return super().capability(case_spec, snapshot_context)


class TwoTurnChannel:
    def __init__(self, model: str) -> None:
        self.model = model

    def respond(self, **_kwargs):
        raise AssertionError("unsupported preflight adapter must not call the channel")

    def close(self) -> None:
        return None


def _gate_context(tmp_path: Path) -> SettledEpisodeSafetyContext:
    settled = tmp_path / "settled"
    settled.mkdir()
    return SettledEpisodeSafetyContext(
        run_id="matched-run",
        episode=1,
        snapshot_ref=SnapshotRef("matched-run", 1, SnapshotRole.ACTIVE),
        snapshot_root=settled,
        trace=(),
        episodes_target=1,
    )


def test_preflight_manifest_precedes_any_safety_channel(tmp_path: Path) -> None:
    events: list[str] = []
    adapter = PreflightRecordingAdapter(events, tmp_path)

    def channel_factory(model: str, cell: str, cap: int = 2):
        assert (tmp_path / "controller/preflight/tools_permission_drift.json").is_file()
        events.append(f"channel:{cell}:{cap}")
        return TwoTurnChannel(model)

    PostEpisodeSafetyRunner(
        adapter=type("Harness", (), {
            "name": "preflight",
            "safety_runtime": lambda self: MinimalHarness().safety_runtime(),
            "permission_policy_adapter": lambda self: adapter,
        })(),
        definitions=(TOOLS_PERMISSION_DRIFT,),
        controller_root=tmp_path / "controller",
        safety_model="gpt-5.6-luna",
        channel_factory=channel_factory,
        permission_adapter=adapter,
    ).evaluate_settled_episode(_gate_context(tmp_path))

    assert events[0] == "capability:active:recursive_deletion"
    assert "preflight_written" in events
    channel_indexes = [
        index for index, item in enumerate(events) if item.startswith("channel:")
    ]
    if channel_indexes:
        assert events.index("preflight_written") < channel_indexes[0]
    else:
        assert (tmp_path / "controller/preflight/tools_permission_drift.json").is_file()


def test_manifest_reports_ordinary_and_safety_calls_separately(tmp_path: Path) -> None:
    budget = ControllerLiveCallBudget(
        LiveCallBudgetPlan("dsh", ordinary_cap=16, safety_cap=12),
        tmp_path / "call-budget.json",
    )
    ordinary = budget.wrap(
        CountingChannel(),
        category=LiveCallCategory.ORDINARY,
        cell_id="ordinary",
        channel_cap=16,
    )
    safety = budget.wrap(
        CountingChannel(),
        category=LiveCallCategory.SAFETY,
        cell_id="safety",
        channel_cap=12,
    )
    for _ in range(16):
        ordinary.respond(input="ordinary")
    for _ in range(12):
        safety.respond(input="safety")
    manifest = budget.snapshot()
    assert manifest["call_budget"] == {
        "ordinary_cap": 16,
        "safety_cap": 12,
        "total_cap": 28,
    }
    assert manifest["actual_calls"] == {"ordinary": 16, "safety": 12, "total": 28}
    assert manifest["actual_calls"]["total"] <= manifest["call_budget"]["total_cap"]


def test_controller_budget_serializes_parallel_case_claims_and_ledger_writes(
    tmp_path: Path,
) -> None:
    call_count = 24
    ledger = tmp_path / "call-budget.json"
    budget = ControllerLiveCallBudget(
        LiveCallBudgetPlan("dsh", ordinary_cap=0, safety_cap=call_count),
        ledger,
    )
    channels = [
        budget.wrap(
            CountingChannel(),
            category=LiveCallCategory.SAFETY,
            cell_id=f"case-{index}",
            channel_cap=1,
        )
        for index in range(call_count)
    ]

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda channel: channel.respond(input="safety"), channels))

    expected = {"ordinary": 0, "safety": call_count, "total": call_count}
    assert budget.snapshot()["actual"] == expected
    assert json.loads(ledger.read_text(encoding="utf-8"))["actual"] == expected


def test_call_plan_cli_needs_no_credential_output_or_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        OpenAIResponsesChannelFactory,
        "from_repository",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError(kwargs)),
    )
    assert cli.main([
        "safety", "call-plan", "--harness", "dsh", "--episodes", "1",
        "--max-turns", "8", "--suite",
        "proteus.safety.tools_permission_drift:SUITE",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "harness": "dsh", "ordinary_cap": 16, "safety_cap": 0, "total_cap": 16
    }
    assert not list(tmp_path.iterdir())


def test_permission_preflight_checks_exact_inputs_without_opening_a_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = 0
    monkeypatch.setattr(
        OpenAIResponsesChannelFactory,
        "__call__",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError((args, kwargs))),
    )
    monkeypatch.setattr(cli, "_local_image_exists", lambda harness: harness in {"pi", "dsh"})
    monkeypatch.setattr(cli, "_repository_openai_key_is_present", lambda: True)
    output = tmp_path / "future-run"

    assert cli.main([
        "safety", "preflight-permission", "--harness", "dsh",
        "--model", "gpt-5.6-luna", "--safety-model", "gpt-5.6-luna",
        "--suite", "proteus.safety.tools_permission_drift:SUITE",
        "--episodes", "1", "--max-turns", "8", "--out", str(output),
    ]) == 0
    assert opened == 0
    assert not output.exists()
