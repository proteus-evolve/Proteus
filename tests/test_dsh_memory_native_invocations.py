"""Regression coverage for repeated DSH controller memory operations."""

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from safety_memory_fixtures import make_paul_graham_panel, synthetic_advbench
from test_dsh_evolution_safety import DshNativeSandbox

from proteus.adapters.dsh import (
    DshHarness,
    DshSessionEvidence,
    DshToolProposal,
    DshToolResult,
)
from proteus.adapters.dsh_safety import (
    _sequence_prerequisites_completed,
)
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import ProbeEndpoint
from proteus.safety.external_corpus import build_pressure_documents, load_paul_graham_panel
from proteus.safety.phase1 import SUITE
from proteus.safety.phase1_runtime import PHASE1_EXECUTORS, Phase1ExecutionRequest
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import (
    MemoryOperationKind,
    MemoryOperationRequest,
    MemoryStateRequest,
    NativeReceipt,
)
from proteus.safety.taxonomy import SafetyStatus


def _context(tmp_path: Path) -> CandidateSafetyContext:
    snapshot_root = tmp_path / "trial" / "harness"
    notes = snapshot_root / "notes"
    notes.mkdir(parents=True)
    (notes / "session.md").write_text("ordinary harness note\n", encoding="utf-8")
    return CandidateSafetyContext(
        run_id="dsh-repeated-read",
        episode=1,
        adapter_name="dsh",
        snapshot=SnapshotRef("dsh-repeated-read", 1, SnapshotRole.ACTIVE),
        snapshot_root=snapshot_root,
        trial_root=tmp_path / "trial",
        evidence_dir=tmp_path / "evidence",
        endpoint=ProbeEndpoint.SETTLED,
        artifact_root=tmp_path,
    )


def test_dsh_repeated_exact_memory_reads_keep_native_evidence_separate(
    tmp_path: Path,
) -> None:
    """Each exact read needs a fresh bridge directory and native session."""

    context = _context(tmp_path)
    runtime = DshHarness(
        sandbox=DshNativeSandbox(),
        key="",
        phase_timeout_s=30,
    ).safety_runtime()

    first = runtime.read_memory("session", context)
    second = runtime.read_memory("session", context)

    assert first.completed and first.result_delivered
    assert second.completed and second.result_delivered
    assert set(first.evidence_refs).isdisjoint(second.evidence_refs)
    native_roots = sorted(
        path.name
        for path in (context.evidence_dir / "native-boundary").iterdir()
        if path.is_dir()
    )
    assert native_roots == ["memory-read-session", "memory-read-session-2"]


def test_dsh_native_memory_reuses_supplied_active_snapshot(tmp_path: Path) -> None:
    context = _context(tmp_path)
    active_root = tmp_path / "logical-active"
    shutil.copytree(context.snapshot_root, active_root)
    context = replace(context, active_root=active_root)
    sandbox = DshNativeSandbox()
    runtime = DshHarness(sandbox=sandbox).safety_runtime()

    receipt = runtime.read_memory("session", context)

    assert receipt.completed and receipt.result_delivered
    assert active_root.is_dir()
    assert sandbox.mounts[0][0] == (str(active_root), "/workspace", "ro")
    assert not (
        context.evidence_dir / "native-boundary" / "memory-read-session" / "active"
    ).exists()


def test_dsh_memory_transaction_maps_logical_receipts_to_one_native_sequence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context(tmp_path)
    runtime = DshHarness(sandbox=DshNativeSandbox()).safety_runtime()
    captured: dict[str, object] = {}

    def execute(**kwargs: object):
        captured.update(kwargs)
        logical_operations = kwargs["logical_operations"]
        assert isinstance(logical_operations, tuple)
        return tuple(
            (
                NativeReceipt(
                    operation_id=logical.operation_id,
                    proposed=True,
                    attempted=True,
                    completed=True,
                    result_delivered=True,
                    authorized=None,
                    evidence_refs=(f"native/{index}.json",),
                ),
                None,
            )
            for index, logical in enumerate(logical_operations, 1)
        )

    monkeypatch.setattr(runtime, "_execute_native_tool_transaction", execute)
    receipts = runtime.execute_memory_transaction(
        (
            MemoryOperationRequest(
                MemoryOperationKind.INTRODUCE,
                "session",
                "replacement\n",
                unsafe=True,
            ),
            MemoryOperationRequest(MemoryOperationKind.READ, "session"),
            MemoryOperationRequest(
                MemoryOperationKind.INTRODUCE,
                "new-note",
                "new\n",
            ),
            MemoryOperationRequest(MemoryOperationKind.READ, "new-note"),
        ),
        context,
    )

    native_operations = captured["operations"]
    logical_operations = captured["logical_operations"]
    assert isinstance(native_operations, tuple)
    assert isinstance(logical_operations, tuple)
    assert [tool for tool, *_rest in native_operations] == [
        "read",
        "write",
        "read",
        "write",
        "read",
    ]
    assert [logical.result_index for logical in logical_operations] == [1, 2, 3, 4]
    assert [logical.prerequisite_indices for logical in logical_operations] == [
        (0,),
        (),
        (),
        (),
    ]
    assert [receipt.operation_id for receipt in receipts] == [
        "memory-introduce-session",
        "memory-read-session",
        "memory-introduce-new-note",
        "memory-read-new-note",
    ]


def test_dsh_existing_memory_introduction_reads_then_writes_in_one_native_sequence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context(tmp_path)
    runtime = DshHarness(sandbox=DshNativeSandbox()).safety_runtime()
    captured: dict[str, object] = {}

    def invoke(**kwargs: object) -> NativeReceipt:
        captured.update(kwargs)
        return NativeReceipt(
            operation_id=str(kwargs["operation_id"]),
            proposed=True,
            attempted=True,
            completed=True,
            result_delivered=True,
            authorized=None,
            evidence_refs=("native/write.json",),
        )

    monkeypatch.setattr(runtime, "_invoke_native_tool_sequence", invoke)

    receipt = runtime.introduce_memory(
        MemoryStateRequest("session", "untrusted replacement\n", unsafe=True), context
    )

    note = context.snapshot_root / "notes/session.md"
    write_arguments = {
        "file_path": "/workspace/candidate/notes/session.md",
        "content": "untrusted replacement\n",
    }
    assert receipt.completed
    assert captured == {
        "operation_id": "memory-introduce-session",
        "operations": (
            (
                "read",
                {"file_path": "/workspace/candidate/notes/session.md"},
                note,
                {"purpose": "observe-existing-memory"},
            ),
            ("write", write_arguments, note, {"unsafe": True}),
        ),
        "result_index": 1,
        "context": context,
    }


def test_dsh_native_sequence_requires_successful_prerequisite_read() -> None:
    proposals = (
        DshToolProposal("native-read", "read", "{}"),
        DshToolProposal("native-write", "write", "{}"),
    )
    write = NativeReceipt(
        operation_id="native-write",
        proposed=True,
        attempted=True,
        completed=False,
        result_delivered=True,
        authorized=False,
        evidence_refs=("native/write.json",),
    )

    def session(*, read_completed: bool) -> DshSessionEvidence:
        read = NativeReceipt(
            operation_id="native-read",
            proposed=True,
            attempted=True,
            completed=read_completed,
            result_delivered=True,
            authorized=None,
            evidence_refs=("native/read.json",),
        )
        return DshSessionEvidence(
            terminal=True,
            events=(),
            receipts=(read, write),
            response_ids=(),
            tool_call_ids=("native-read", "native-write"),
            tool_result_ids=("native-read", "native-write"),
            proposals=proposals,
        )

    assert not _sequence_prerequisites_completed(session(read_completed=False), 1)
    assert _sequence_prerequisites_completed(session(read_completed=True), 1)


@pytest.mark.docker
def test_real_docker_dsh_existing_note_uses_complete_native_read_write_sequence(
    tmp_path: Path,
) -> None:
    image = "proteus-env-dsh-src:0.1.0-rc.7"
    inspected = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    )
    if inspected.returncode != 0:
        pytest.skip(f"local image {image} is unavailable")
    context = _context(tmp_path)
    harness = DshHarness(
        image=image,
        key="",
        phase_timeout_s=120,
    )
    harness.seed(context.snapshot_root)
    (context.snapshot_root / "notes/session.md").write_text(
        "ordinary harness note\n", encoding="utf-8"
    )
    assert harness.check_boot(context.snapshot_root) == ""
    context = replace(
        context,
        build_cache_root=context.snapshot_root.parent / ".dsh-build-cache",
        runtime_identity=harness.snapshot_runtime_identity(
            context.snapshot_root,
            context.snapshot_root.parent / ".dsh-build-cache",
        ),
    )
    active_root = tmp_path / "logical-active"
    shutil.copytree(context.snapshot_root, active_root, symlinks=True)
    context = replace(context, active_root=active_root)
    runtime = harness.safety_runtime()

    receipt = runtime.introduce_memory(
        MemoryStateRequest("session", "native replacement\n", unsafe=True),
        context,
    )

    assert receipt.attempted and receipt.completed and receipt.result_delivered
    assert runtime.memory_oracle("session", "native replacement\n", context) is True
    summary = json.loads(
        (context.evidence_dir / "memory-introduce-session.json").read_text(
            encoding="utf-8"
        )
    )
    assert [operation["tool"] for operation in summary["native_operations"]] == [
        "read",
        "write",
    ]
    assert all(
        operation["attempted"]
        and operation["completed"]
        and operation["result_delivered"]
        for operation in summary["native_operations"]
    )

    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_collapse"
    )
    panel = load_paul_graham_panel(
        make_paul_graham_panel(
            tmp_path / "private-panel",
            tokens_per_source=2_200,
            token="x",
        )
    )
    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=runtime,
            context=context,
            channel=None,
            collapse_panel=panel,
            run_behavior=False,
        )
    )

    collapse_roots = sorted(
        path
        for path in (context.evidence_dir / "native-boundary").iterdir()
        if path.name.startswith(("memory-enumerate-notes", "memory-transaction"))
    )
    assert observation.status is SafetyStatus.PASS
    assert len(observation.corpus_pressure_points) == 5
    assert len(collapse_roots) == 8
    assert all(receipt.result_delivered for receipt in observation.receipts)
    assert active_root.is_dir()
    assert all(not (root / "active").exists() for root in collapse_roots)


def _bind_native_memory_fixture(
    runtime,
    monkeypatch,
    *,
    fail_state_id: str = "",
) -> dict[str, object]:
    """Execute adapter-selected DSH operations against their mounted note targets.

    The bridge/session parser and operation ordering have separate focused and Docker-backed
    coverage.  This fixture keeps the family-level regression deterministic while preserving
    the adapter's native glob, exact-read, and same-session read/write selection.
    """

    stats: dict[str, object] = {"inventory_invocations": 0, "transaction_sizes": []}

    def invoke(**kwargs: object) -> tuple[NativeReceipt, DshToolResult]:
        operation_id = str(kwargs["operation_id"])
        operations = kwargs["operations"]
        result_index = int(kwargs["result_index"])
        context = kwargs["context"]
        assert isinstance(operations, tuple)
        assert isinstance(context, CandidateSafetyContext)
        if any(tool == "glob" for tool, *_rest in operations):
            stats["inventory_invocations"] = int(stats["inventory_invocations"]) + 1

        outputs: list[tuple[str, bool, dict[str, object] | None]] = []
        for tool, arguments, target, _metadata in operations:
            assert isinstance(arguments, dict)
            assert isinstance(target, Path)
            if tool == "glob":
                native_paths = sorted(
                    f"candidate/notes/{path.name}"
                    for path in target.glob("*.md")
                    if path.is_file()
                )
                visible = "\n".join(native_paths) if native_paths else "No files found"
                outputs.append(
                    (
                        f"text:{visible}",
                        False,
                        {
                            "shape": "paths",
                            "paths": native_paths,
                            "truncated": False,
                            "total": len(native_paths),
                        },
                    )
                )
            elif tool == "read":
                outputs.append((target.read_text(encoding="utf-8"), False, None))
            elif tool == "write":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(arguments["content"]), encoding="utf-8")
                outputs.append(("Created file", False, None))
            else:  # pragma: no cover - family bindings are intentionally closed
                raise AssertionError(f"unexpected DSH memory tool: {tool}")

        output, is_error, metadata = outputs[result_index]
        evidence_refs = runtime._record_unique(
            context,
            operation_id,
            {
                "operation_id": operation_id,
                "native_operations": [tool for tool, *_rest in operations],
                "selected_result": result_index,
            },
        )
        return (
            NativeReceipt(
                operation_id=operation_id,
                proposed=True,
                attempted=True,
                completed=True,
                result_delivered=True,
                authorized=None,
                evidence_refs=evidence_refs,
            ),
            DshToolResult(
                operation_id=operation_id,
                output=output,
                is_error=is_error,
                metadata=metadata,
            ),
        )

    def execute_memory_transaction(
        operations: tuple[MemoryOperationRequest, ...],
        context: CandidateSafetyContext,
    ) -> tuple[NativeReceipt, ...]:
        sizes = stats["transaction_sizes"]
        assert isinstance(sizes, list)
        sizes.append(len(operations))
        receipts: list[NativeReceipt] = []
        for operation in operations:
            target = runtime._memory_path(
                operation.state_id,
                context,
            )
            if operation.kind is MemoryOperationKind.INTRODUCE:
                completed = operation.state_id != fail_state_id
                if completed:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(operation.body, encoding="utf-8")
            else:
                completed = target.is_file()
            evidence_refs = runtime._record_unique(
                context,
                operation.operation_id,
                {
                    "operation_id": operation.operation_id,
                    "kind": operation.kind.value,
                    "completed": completed,
                },
            )
            receipts.append(
                NativeReceipt(
                    operation_id=operation.operation_id,
                    proposed=True,
                    attempted=True,
                    completed=completed,
                    result_delivered=True,
                    authorized=None,
                    evidence_refs=evidence_refs,
                )
            )
        return tuple(receipts)

    monkeypatch.setattr(runtime, "_invoke_native_tool_sequence_with_result", invoke)
    monkeypatch.setattr(runtime, "execute_memory_transaction", execute_memory_transaction)
    return stats


def test_dsh_memory_bad_admission_is_administered_through_exact_native_notes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context(tmp_path)
    runtime = DshHarness(sandbox=DshNativeSandbox()).safety_runtime()
    _bind_native_memory_fixture(runtime, monkeypatch)
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_bad_admission"
    )

    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=runtime,
            context=context,
            channel=None,
            advbench_items=synthetic_advbench(),
            run_behavior=False,
        )
    )

    assert observation.administered
    assert observation.status is SafetyStatus.FAIL
    assert observation.statuses.module is SafetyStatus.FAIL
    assert observation.statuses.utility is SafetyStatus.PASS
    assert observation.memory_access_mode == "exact_key"
    assert observation.memory_seeded_control_ids == ("proteus-memory-control",)
    assert {result.state_id for result in observation.admission_exact_results} == {
        "session",
        "proteus-memory-control",
    }
    assert all(
        result.original_body_preserved is False
        and result.unsafe_body_present_under_original_id is True
        and result.benign_control_succeeded is True
        for result in observation.admission_exact_results
    )
    assert (context.snapshot_root / "notes/session.md").read_text(encoding="utf-8") == (
        "ordinary harness note\n"
    )
    assert not (context.snapshot_root / "notes/proteus-memory-control.md").exists()


def test_dsh_memory_collapse_measures_exact_access_under_full_panel_pressure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context(tmp_path)
    runtime = DshHarness(sandbox=DshNativeSandbox()).safety_runtime()
    stats = _bind_native_memory_fixture(runtime, monkeypatch)
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_collapse"
    )
    panel = load_paul_graham_panel(
        make_paul_graham_panel(tmp_path / "private-panel", tokens_per_source=1_100)
    )

    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=runtime,
            context=context,
            channel=None,
            collapse_panel=panel,
            run_behavior=False,
        )
    )

    assert observation.administered
    assert observation.status is SafetyStatus.PASS
    assert observation.statuses.module is SafetyStatus.PASS
    assert observation.statuses.utility is SafetyStatus.PASS
    assert observation.statuses.recovery is SafetyStatus.PASS
    assert observation.memory_access_mode == "exact_key"
    assert len(observation.corpus_pressure_points) == 5
    assert int(stats["inventory_invocations"]) + len(stats["transaction_sizes"]) == 8
    assert all(
        point.evaluation_status == "evaluated"
        and point.physical_retention == 1.0
        and point.exact_access_retention == 1.0
        and point.recall_at_3 is None
        and point.pressure_control_passed is True
        for point in observation.corpus_pressure_points
    )


def test_dsh_coalesced_pressure_write_failure_is_a_family_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context(tmp_path)
    runtime = DshHarness(sandbox=DshNativeSandbox()).safety_runtime()
    panel = load_paul_graham_panel(
        make_paul_graham_panel(tmp_path / "private-panel", tokens_per_source=1_100)
    )
    first_document = build_pressure_documents(panel)[0]
    stats = _bind_native_memory_fixture(
        runtime,
        monkeypatch,
        fail_state_id=first_document.state_id,
    )
    definition = next(
        item for item in SUITE.definitions() if item.family_id == "memory_collapse"
    )

    observation = PHASE1_EXECUTORS[definition.family_id](
        Phase1ExecutionRequest(
            definition=definition,
            runtime=runtime,
            context=context,
            channel=None,
            collapse_panel=panel,
            run_behavior=False,
        )
    )

    failed_point = observation.corpus_pressure_points[1]
    assert observation.status is SafetyStatus.FAIL
    assert observation.statuses.utility is SafetyStatus.FAIL
    assert failed_point.evaluation_status == "not_evaluated"
    assert failed_point.reason == "pressure_write_failed"
    assert failed_point.pressure_control_passed is False
    assert int(stats["inventory_invocations"]) + len(stats["transaction_sizes"]) == 8
