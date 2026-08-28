"""Aki exact-key memory inventory regression coverage."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from proteus.adapters.aki_live_worker import AkiWorkerResult, BoundaryRecord
from proteus.adapters.aki_safety import AkiSafetyRuntime
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.evidence import ProbeEndpoint
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import MemoryAccessMode, MemoryRecordKind


def _context(tmp_path: Path) -> CandidateSafetyContext:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    return CandidateSafetyContext(
        run_id="aki-memory-inventory",
        episode=1,
        adapter_name="aki",
        snapshot=SnapshotRef("aki-memory-inventory", 1, SnapshotRole.ACTIVE),
        snapshot_root=snapshot_root,
        trial_root=tmp_path / "trial",
        evidence_dir=tmp_path / "evidence",
        endpoint=ProbeEndpoint.SETTLED,
    )


def _native_worker_result(
    *,
    channel,
    result: object,
) -> AkiWorkerResult:
    return AkiWorkerResult(
        terminal=True,
        structural_bijection_complete=True,
        listener_threads_stopped=True,
        boundaries=(
            BoundaryRecord(
                call_id=channel.operation_id,
                tool_name=channel.tool,
                arguments=channel.arguments,
                proposed=True,
                authorized=True,
                attempted=True,
                completed=True,
                result_delivered=True,
                result=result,
            ),
        ),
    )


def _listed_memory(
    name: str = "observations",
    *,
    filename: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "description": "native description",
        "filename": filename if filename is not None else f"{name}.md",
        "updated_at": "2026-08-28T00:00:00Z",
        "source": "agent-authored",
        "trust": "untrusted",
        "content_role": "data",
    }


def _native_read_data(entry: dict[str, object], body: str) -> dict[str, object]:
    return {
        "success": True,
        "data": {
            key: entry[key]
            for key in (
                "name",
                "description",
                "updated_at",
                "source",
                "trust",
                "content_role",
            )
        }
        | {"body": body},
    }


def test_aki_inventory_uses_native_list_and_exact_key_records(tmp_path: Path, monkeypatch) -> None:
    context = _context(tmp_path)
    memory = context.snapshot_root / "memory"
    memory.mkdir()
    (memory / "observations.md").write_text("host body must not be used\n", encoding="utf-8")
    (memory / "unlisted-malformed.md").write_text("not native ordinary memory\n", encoding="utf-8")
    runtime = AkiSafetyRuntime(SimpleNamespace(container=object()))
    calls: list[tuple[str, str, str]] = []
    entry = _listed_memory()

    def run_worker(*, trial_name, channel, **_kwargs):
        calls.append((trial_name, channel.operation_id, channel.tool))
        result = (
            {"success": True, "data": {"count": 1, "memories": [entry]}}
            if channel.tool == "memory_list"
            else _native_read_data(entry, "native body\n")
        )
        return _native_worker_result(
            channel=channel,
            result=result,
        )

    monkeypatch.setattr(runtime, "_run_worker", run_worker)

    records = runtime.enumerate_ordinary_memory(context)

    assert runtime.memory_access_mode is MemoryAccessMode.EXACT_KEY
    assert calls == [
        ("operation-memory-list", "memory-list", "memory_list"),
        ("operation-memory-read-observations", "memory-read-observations", "memory_read"),
    ]
    assert len(records) == 1
    record = records[0]
    assert record.state_id == "observations"
    assert record.body == "native body\n"
    # These labels are received from Aki's native list result, rather than from
    # the forged frontmatter in the snapshot file.
    assert record.source == "agent-authored"
    assert record.trust == "untrusted"
    assert record.lookup_query is None
    assert record.record_kind is MemoryRecordKind.NATURAL
    inventory = json.loads((context.evidence_dir / "ordinary-memory-inventory.json").read_text())
    assert inventory["native_list_evidence_refs"] == ["memory-list.json"]
    assert inventory["memory_files"] == ["observations.md"]
    assert inventory["native_memory_reads"][0]["evidence_refs"] == [
        "memory-read-observations.json"
    ]


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"success": False, "data": {"memories": [], "count": 0}}, "unsuccessful"),
        ({"success": True, "data": {"memories": [], "count": 1}}, "count"),
        (
            {
                "success": True,
                "data": {
                    "count": 1,
                    "memories": [
                        {
                            "name": "../escape",
                            "filename": "../escape.md",
                            "source": "agent-authored",
                            "trust": "untrusted",
                        }
                    ],
                },
            },
            "path-free",
        ),
        (
            {
                "success": True,
                "data": {
                    "count": 1,
                    "memories": [
                        {
                            "name": "other",
                            "filename": "observations.md",
                            "source": "agent-authored",
                            "trust": "untrusted",
                        }
                    ],
                },
            },
            "filename",
        ),
    ],
)
def test_aki_inventory_rejects_malformed_native_listing(
    tmp_path: Path,
    monkeypatch,
    result: object,
    message: str,
) -> None:
    context = _context(tmp_path)
    runtime = AkiSafetyRuntime(SimpleNamespace(container=object()))

    def run_worker(*, channel, **_kwargs):
        return _native_worker_result(channel=channel, result=result)

    monkeypatch.setattr(runtime, "_run_worker", run_worker)

    with pytest.raises(RuntimeError, match=message):
        runtime.enumerate_ordinary_memory(context)


def test_aki_inventory_rejects_duplicate_or_missing_native_listed_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context(tmp_path)
    runtime = AkiSafetyRuntime(SimpleNamespace(container=object()))
    entries = [_listed_memory(), _listed_memory()]

    def duplicate_worker(*, channel, **_kwargs):
        return _native_worker_result(
            channel=channel,
            result={"success": True, "data": {"memories": entries, "count": len(entries)}},
        )

    monkeypatch.setattr(runtime, "_run_worker", duplicate_worker)
    with pytest.raises(RuntimeError, match="duplicate"):
        runtime.enumerate_ordinary_memory(context)

    def missing_file_worker(*, channel, **_kwargs):
        return _native_worker_result(
            channel=channel,
            result={"success": True, "data": {"memories": entries[:1], "count": 1}},
        )

    monkeypatch.setattr(runtime, "_run_worker", missing_file_worker)
    with pytest.raises(RuntimeError, match="file is missing"):
        runtime.enumerate_ordinary_memory(context)


def test_aki_inventory_rejects_native_read_metadata_mismatch(tmp_path: Path, monkeypatch) -> None:
    context = _context(tmp_path)
    memory = context.snapshot_root / "memory"
    memory.mkdir()
    (memory / "observations.md").write_text("direct-child only\n", encoding="utf-8")
    runtime = AkiSafetyRuntime(SimpleNamespace(container=object()))
    entry = _listed_memory()

    def run_worker(*, channel, **_kwargs):
        if channel.tool == "memory_list":
            result = {"success": True, "data": {"count": 1, "memories": [entry]}}
        else:
            result = _native_read_data(entry, "native body\n")
            result["data"]["updated_at"] = "mismatched"  # type: ignore[index]
        return _native_worker_result(channel=channel, result=result)

    monkeypatch.setattr(runtime, "_run_worker", run_worker)

    with pytest.raises(RuntimeError, match="updated_at does not match list"):
        runtime.enumerate_ordinary_memory(context)


def test_aki_repeated_native_operations_keep_logical_receipts_and_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    context = _context(tmp_path)
    runtime = AkiSafetyRuntime(SimpleNamespace(container=object()))
    calls: list[tuple[str, str]] = []

    def run_worker(*, trial_name, channel, **_kwargs):
        calls.append((trial_name, channel.operation_id))
        return _native_worker_result(
            channel=channel,
            result={"success": True, "data": {"name": "repeat"}},
        )

    monkeypatch.setattr(runtime, "_run_worker", run_worker)

    first = runtime.read_memory("repeat", context)
    second = runtime.read_memory("repeat", context)

    assert first.operation_id == second.operation_id == "memory-read-repeat"
    assert calls == [
        ("operation-memory-read-repeat", "memory-read-repeat"),
        ("operation-memory-read-repeat-2", "memory-read-repeat-2"),
    ]
    assert (context.evidence_dir / "memory-read-repeat.json").is_file()
    assert (context.evidence_dir / "memory-read-repeat-2.json").is_file()
