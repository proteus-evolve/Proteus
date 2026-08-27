from __future__ import annotations

import csv
import random
from pathlib import Path

from safety_memory_fixtures import synthetic_advbench
from test_evolution_safety_contracts import _context
from test_evolution_safety_gate import (
    GateFixtureAdapter,
    RecordingPairedPermissionPolicyExecutor,
    _gate_context,
)

from proteus.adapters.minimal import MinimalHarness
from proteus.adapters.minimal_safety import MinimalSafetyRuntime
from proteus.safety.advbench import SAMPLE_SIZE, AdvBenchItem, sample_advbench
from proteus.safety.collapse_filler import (
    MAX_TOKENS,
    MIN_TOKENS,
    generate_unrelated_document,
    parse_collapse_episodes,
)
from proteus.safety.gate import GateRunner
from proteus.safety.phase1 import SUITE
from proteus.safety.phase1_runtime import (
    ADMISSION_MEMORY_ID,
    PHASE1_EXECUTORS,
    Phase1ExecutionRequest,
)
from proteus.safety.taxonomy import SafetyStatus


def test_advbench_sample_is_seeded_and_sized(tmp_path: Path) -> None:
    catalog = tuple(
        AdvBenchItem(goal=f"goal-{index}", target=f"target-{index}", index=index)
        for index in range(40)
    )
    first = sample_advbench(catalog, n=SAMPLE_SIZE, seed=7)
    second = sample_advbench(catalog, n=SAMPLE_SIZE, seed=7)
    other = sample_advbench(catalog, n=SAMPLE_SIZE, seed=8)

    assert len(first) == SAMPLE_SIZE
    assert first == second
    assert first != other


def test_advbench_csv_loader_reads_goal_and_target(tmp_path: Path) -> None:
    path = tmp_path / "harmful_behaviors.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=("goal", "target"))
        writer.writeheader()
        for index in range(24):
            writer.writerow({"goal": f"goal {index}", "target": f"Sure, here is {index}"})
    from proteus.safety.advbench import _records

    items = _records(path)
    assert len(items) == 24
    assert items[0].goal == "goal 0"
    assert items[3].target == "Sure, here is 3"


def test_unrelated_filler_stays_in_token_range() -> None:
    for seed in range(8):
        text = generate_unrelated_document(random.Random(seed))
        tokens = text.split()
        assert MIN_TOKENS <= len(tokens) <= MAX_TOKENS
        assert "write" not in text
        assert "ignore" not in text


def test_parse_collapse_episodes_accepts_last() -> None:
    assert parse_collapse_episodes("1,last", 10) == frozenset({1, 10})
    assert parse_collapse_episodes("last", 1) == frozenset({1})


def test_parse_collapse_episodes_every_five() -> None:
    assert parse_collapse_episodes("every:5", 20) == frozenset({1, 5, 10, 15, 20})
    assert parse_collapse_episodes("1,5,10,15,20", 20) == frozenset({1, 5, 10, 15, 20})
    assert parse_collapse_episodes("every:5", 7) == frozenset({1, 5})
    assert parse_collapse_episodes("every:5", 1) == frozenset({1})


def test_unselected_collapse_episode_is_skipped(tmp_path: Path) -> None:
    adapter = GateFixtureAdapter()
    gate = GateRunner(
        adapter=adapter,
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=None,
        permission_executor=RecordingPairedPermissionPolicyExecutor(),
        collapse_episodes=frozenset({9}),
        advbench_items=synthetic_advbench(),
    )

    decision = gate.evaluate(_gate_context(tmp_path))
    root = (tmp_path / "controller" / decision.decision_ref).parent
    collapse = (
        root / "families" / "memory_collapse" / "candidate.json"
    ).read_text(encoding="utf-8")

    assert "episode_not_selected" in collapse
    assert ("memory_collapse", "candidate") not in adapter.memory_endpoint_calls
    assert ("memory_bad_admission", "candidate") in adapter.memory_endpoint_calls
    assert decision.status != SafetyStatus.ERROR.value


def test_admission_injects_retrieved_bundle_into_live_prompts(tmp_path: Path) -> None:
    items = synthetic_advbench(2)

    class CapturePrompts(MinimalSafetyRuntime):
        def __init__(self, harness) -> None:
            super().__init__(harness)
            self.prompts: dict[str, str] = {}

        def run_safety_episode(self, prompts, context, channel):
            self.prompts = dict(prompts)
            return super().run_safety_episode(prompts, context, channel)

    runtime = CapturePrompts(MinimalHarness())
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )
    PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=runtime,
            context=_context(tmp_path, "injected-prompts"),
            channel=None,
            advbench_items=items,
        )
    )
    observe = runtime.prompts["observe"]
    assert f"memory named {ADMISSION_MEMORY_ID}" in observe
    assert "----- BEGIN MEMORY -----" in observe
    for item in items:
        assert item.goal in observe
        assert item.goal in runtime.prompts["act"]


def test_admission_writes_all_advbench_goals_under_the_benign_name(tmp_path: Path) -> None:
    items = synthetic_advbench()
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )
    context = _context(tmp_path, "shared-name")
    PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MinimalHarness().safety_runtime(),
            context=context,
            channel=None,
            advbench_items=items,
        )
    )
    notes = context.snapshot_root / "notes"
    shared = notes / f"{ADMISSION_MEMORY_ID}.md"
    text = shared.read_text(encoding="utf-8")

    assert shared.is_file()
    assert list(notes.glob("advbench-*.md")) == []
    for item in items:
        assert item.goal in text
