from __future__ import annotations

import json
from pathlib import Path

import pytest

from proteus.adapters.dsh import DshHarness, DshToolResult
from proteus.adapters.dsh_safety import DshSafetyRuntime
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import ProbeEndpoint
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import (
    MemoryAccessMode,
    MemoryRecordKind,
    MemoryStateRequest,
    NativeReceipt,
)


def _context(tmp_path: Path) -> CandidateSafetyContext:
    snapshot_root = tmp_path / "trial" / "harness"
    snapshot_root.mkdir(parents=True)
    return CandidateSafetyContext(
        run_id="dsh-memory-run",
        episode=1,
        adapter_name="dsh",
        snapshot=SnapshotRef("dsh-memory-run", 1, SnapshotRole.ACTIVE),
        snapshot_root=snapshot_root,
        trial_root=tmp_path / "trial",
        evidence_dir=tmp_path / "evidence",
        endpoint=ProbeEndpoint.SETTLED,
    )


def _complete_receipt(operation_id: str) -> NativeReceipt:
    return NativeReceipt(
        operation_id=operation_id,
        proposed=True,
        attempted=True,
        completed=True,
        result_delivered=True,
        authorized=None,
        evidence_refs=(f"evidence/{operation_id}.json",),
    )


def test_dsh_introduces_memory_through_native_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = DshSafetyRuntime(DshHarness())
    context = _context(tmp_path)
    captured: dict[str, object] = {}

    def invoke(**kwargs: object) -> NativeReceipt:
        captured.update(kwargs)
        return _complete_receipt(str(kwargs["operation_id"]))

    monkeypatch.setattr(runtime, "_invoke_native_tool", invoke)

    receipt = runtime.introduce_memory(
        MemoryStateRequest("reference-note", "keep this note\n", unsafe=False), context
    )

    assert runtime.memory_access_mode is MemoryAccessMode.EXACT_KEY
    assert receipt.completed
    assert captured == {
        "operation_id": "memory-introduce-reference-note",
        "tool": "write",
        "arguments": {
            "file_path": "/workspace/candidate/notes/reference-note.md",
            "content": "keep this note\n",
        },
        "target": context.snapshot_root / "notes/reference-note.md",
        "context": context,
        "metadata": {"unsafe": False},
    }


def test_dsh_inventory_is_top_level_natural_notes_with_native_glob_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = DshSafetyRuntime(DshHarness())
    context = _context(tmp_path)
    notes = context.snapshot_root / "notes"
    notes.mkdir()
    (notes / "operator-note.md").write_text("preserve this\n", encoding="utf-8")
    nested = notes / "archive"
    nested.mkdir()
    (nested / "ignored.md").write_text("not an ordinary top-level note\n", encoding="utf-8")
    invocations: list[dict[str, object]] = []

    def invoke(**kwargs: object) -> tuple[NativeReceipt, DshToolResult]:
        invocations.append(kwargs)
        operation_id = str(kwargs["operation_id"])
        return _complete_receipt(operation_id), DshToolResult(
            operation_id=operation_id,
            output="text:candidate/notes/operator-note.md",
            is_error=False,
            metadata={
                "shape": "paths",
                "paths": ["candidate/notes/operator-note.md"],
                "truncated": False,
                "total": 1,
            },
        )

    monkeypatch.setattr(runtime, "_invoke_native_tool_with_result", invoke)

    records = runtime.enumerate_ordinary_memory(context)

    assert invocations == [
        {
            "operation_id": "memory-enumerate-notes",
            "tool": "glob",
            "arguments": {
                "pattern": "candidate/notes/*.md",
                "path": "/workspace",
            },
            "target": notes,
            "context": context,
        }
    ]
    assert len(records) == 1
    record = records[0]
    assert record.state_id == "operator-note"
    assert record.body == "preserve this\n"
    assert record.source == "notes"
    assert record.trust == "mutable"
    assert record.lookup_query is None
    assert record.record_kind is MemoryRecordKind.NATURAL

    summary = json.loads(
        (context.evidence_dir / "memory-enumerate-notes-summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["native_receipt_refs"] == [
        "evidence/memory-enumerate-notes.json"
    ]
    assert summary["native_paths"] == ["candidate/notes/operator-note.md"]
    assert summary["state_ids"] == ["operator-note"]


@pytest.mark.parametrize(
    ("paths", "truncated"),
    [
        (["candidate/notes/archive/nested.md"], False),
        (["candidate/notes/operator-note.md"], True),
        (["candidate/notes/missing.md"], False),
    ],
)
def test_dsh_inventory_rejects_incomplete_or_unbound_native_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paths: list[str],
    truncated: bool,
) -> None:
    runtime = DshSafetyRuntime(DshHarness())
    context = _context(tmp_path)
    notes = context.snapshot_root / "notes"
    notes.mkdir()
    (notes / "operator-note.md").write_text("preserve this\n", encoding="utf-8")

    def invoke(**kwargs: object) -> tuple[NativeReceipt, DshToolResult]:
        operation_id = str(kwargs["operation_id"])
        output = "\n".join(paths) if paths else "No files found"
        return _complete_receipt(operation_id), DshToolResult(
            operation_id=operation_id,
            output=f"text:{output}",
            is_error=False,
            metadata={
                "shape": "paths",
                "paths": paths,
                "truncated": truncated,
                "total": len(paths),
            },
        )

    monkeypatch.setattr(runtime, "_invoke_native_tool_with_result", invoke)

    with pytest.raises(RuntimeError, match="native DSH"):
        runtime.enumerate_ordinary_memory(context)


def test_dsh_empty_notes_inventory_writes_a_summary_without_a_fake_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = DshSafetyRuntime(DshHarness())
    context = _context(tmp_path)
    invoked = False

    def invoke(**_kwargs: object) -> NativeReceipt:
        nonlocal invoked
        invoked = True
        raise AssertionError("a missing notes directory has no native glob route")

    monkeypatch.setattr(runtime, "_invoke_native_tool", invoke)

    assert runtime.enumerate_ordinary_memory(context) == ()
    assert not invoked
    summary = json.loads(
        (context.evidence_dir / "memory-enumerate-notes-summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary == {
        "native_receipt_completed": False,
        "native_receipt_refs": [],
        "notes_directory_present": False,
        "notes_root": "notes",
        "native_paths": [],
        "state_ids": [],
    }
