from __future__ import annotations

import csv
import json
import random
from pathlib import Path

from safety_memory_fixtures import HARNESS_NOTE_ID, synthetic_advbench
from test_evolution_safety_contracts import _context
from test_evolution_safety_gate import (
    GateFixtureAdapter,
    RecordingSnapshotPermissionExecutor,
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
from proteus.safety.gate import PostEpisodeSafetyRunner
from proteus.safety.phase1 import SUITE
from proteus.safety.phase1_runtime import PHASE1_EXECUTORS, Phase1ExecutionRequest
from proteus.safety.schedule import EveryEpisode, ExplicitEpisodes, SafetySuiteSchedule
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


def test_cli_every_n_uses_everyn_schedule_object() -> None:
    from types import SimpleNamespace

    from proteus.cli import _collapse_schedule
    from proteus.safety.schedule import EveryN, ExplicitEpisodes

    assert _collapse_schedule(SimpleNamespace(collapse_episodes="every:5", episodes=20)) == EveryN(5)
    assert _collapse_schedule(SimpleNamespace(collapse_episodes="every:1", episodes=4)) == EveryN(1)
    explicit = _collapse_schedule(SimpleNamespace(collapse_episodes="1,last", episodes=10))
    assert explicit == ExplicitEpisodes({1, 10})


def test_unselected_collapse_episode_is_skipped(tmp_path: Path) -> None:
    adapter = GateFixtureAdapter()
    gate = PostEpisodeSafetyRunner(
        adapter=adapter,
        definitions=SUITE.definitions(),
        controller_root=tmp_path / "controller",
        safety_model="",
        channel_factory=None,
        permission_executor=RecordingSnapshotPermissionExecutor(),
        schedule=SafetySuiteSchedule(
            memory_bad_admission=EveryEpisode(),
            memory_collapse=ExplicitEpisodes({9}),
            tools_permission_drift=EveryEpisode(),
        ),
        advbench_items=synthetic_advbench(),
    )

    decision = gate.evaluate_settled_episode(_gate_context(tmp_path))
    root = (tmp_path / "controller" / decision.decision_ref).parent
    collapse = json.loads(
        (root / "memory_collapse" / "result.json").read_text(encoding="utf-8")
    )

    assert collapse["execution"]["schedule_status"] == "not_scheduled"
    assert ("memory_collapse", "active") not in adapter.memory_endpoint_calls
    assert ("memory_bad_admission", "active") in adapter.memory_endpoint_calls
    assert decision.status != SafetyStatus.ERROR.value


def test_empty_snapshot_plants_goal_named_benign_and_a_near_adversarial(
    tmp_path: Path,
) -> None:
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )
    context = _context(tmp_path, "empty-memory")
    for path in (context.snapshot_root / "notes").glob("*.md"):
        path.unlink()
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MinimalHarness().safety_runtime(),
            context=context,
            channel=None,
            advbench_items=synthetic_advbench(),
        )
    )
    notes = context.snapshot_root / "notes"
    stems = {path.stem for path in notes.glob("*.md")}
    assert observation.administered
    assert observation.statuses.module is SafetyStatus.FAIL
    assert observation.statuses.utility is SafetyStatus.PASS
    assert "session-note" in stems
    assert observation.unsafe_states[0].state_id != "session-note"
    assert observation.unsafe_states[0].state_id in stems


def test_admission_uses_safety_channel_to_rewrite_the_selected_name(
    tmp_path: Path,
) -> None:
    class NameChannel:
        model = "gpt-5.6-luna"

        def respond(self, **kwargs):
            del kwargs
            from proteus.safety.live import LiveCallProvenance, LiveModelResponse

            provenance = LiveCallProvenance(
                call_id="rename-1",
                response_id="rename-1",
                configured_model=self.model,
                response_model=self.model,
            )
            return LiveModelResponse(
                response_id=provenance.response_id,
                model=self.model,
                output_text="session-notes",
                tool_calls=(),
                provenance=provenance,
            )

        def close(self) -> None:
            return None

    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )
    context = _context(tmp_path, "renamed")
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MinimalHarness().safety_runtime(),
            context=context,
            channel=NameChannel(),
            advbench_items=synthetic_advbench(2),
        )
    )
    assert observation.unsafe_states[0].state_id == "session-notes"
    assert (context.snapshot_root / "notes" / "session.md").read_text(
        encoding="utf-8"
    ) == "ordinary harness note\n"


def test_admission_episode_does_not_paste_the_bundle(tmp_path: Path) -> None:
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
            context=_context(tmp_path, "ordinary-prompts"),
            channel=None,
            advbench_items=items,
        )
    )
    observe = runtime.prompts["observe"]
    assert "Inspect your notes and memory" in observe
    assert "BEGIN MEMORY" not in observe
    for item in items:
        assert item.goal not in observe
        assert item.goal not in runtime.prompts["act"]


def test_admission_writes_all_advbench_goals_under_the_benign_name(tmp_path: Path) -> None:
    items = synthetic_advbench()
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )
    context = _context(tmp_path, "shared-name")
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=MinimalHarness().safety_runtime(),
            context=context,
            channel=None,
            advbench_items=items,
        )
    )
    notes = context.snapshot_root / "notes"
    original = notes / f"{HARNESS_NOTE_ID}.md"
    adversarial = notes / f"{observation.unsafe_states[0].state_id}.md"

    assert original.read_text(encoding="utf-8") == "ordinary harness note\n"
    assert observation.unsafe_states[0].state_id != HARNESS_NOTE_ID
    assert list(notes.glob("advbench-*.md")) == []
    for item in items:
        assert item.goal in adversarial.read_text(encoding="utf-8")
