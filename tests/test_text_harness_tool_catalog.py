"""Native callable catalogs for harnesses whose ordinary actions are text only."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from proteus.adapters.llm import LLMHarness
from proteus.adapters.minimal import MinimalHarness
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.permission_adapter import (
    PermissionSnapshotContext,
    UnsupportedPermissionPolicyAdapter,
)
from proteus.safety.permission_cases import PERMISSION_CASE_SPECS
from proteus.safety.runtime import RuntimeKind


def _context(tmp_path: Path, harness_name: str) -> PermissionSnapshotContext:
    artifact_root = tmp_path / "artifacts"
    snapshot_root = tmp_path / "snapshot"
    (snapshot_root / "tools").mkdir(parents=True)
    (snapshot_root / "tools" / "file_only.py").write_text(
        "def file_only():\n    return 'not registered'\n", encoding="utf-8"
    )
    return PermissionSnapshotContext(
        snapshot=SnapshotRef(f"{harness_name}-catalog", 2, SnapshotRole.CANDIDATE),
        snapshot_root=snapshot_root,
        trial_root=artifact_root / "trials" / harness_name,
        evidence_dir=artifact_root / "trials" / harness_name / "raw",
        artifact_root=artifact_root,
    )


@pytest.mark.parametrize(
    ("harness", "loader_id", "runtime_phrase"),
    (
        (
            MinimalHarness(),
            "minimal.deterministic_policy_actions",
            "deterministic policy",
        ),
        (LLMHarness(), "llm.json_text_actions", "JSON text actions"),
    ),
)
def test_text_harnesses_record_empty_native_catalogs_not_authored_tool_files(
    tmp_path: Path,
    harness: MinimalHarness | LLMHarness,
    loader_id: str,
    runtime_phrase: str,
) -> None:
    context = _context(tmp_path, harness.name)
    adapter = harness.permission_policy_adapter()

    catalog = adapter.collect_native_tool_catalog(context)

    assert catalog is not None
    assert catalog.snapshot == context.snapshot
    assert catalog.loader_id == loader_id
    assert catalog.tools == ()
    assert adapter.native_tool_catalog_reason(context.snapshot) == ""
    # Native callable inventory is empty even though the ordinary text dispatcher
    # exercises two fixed file-boundary cases.  Authored tools/*.py never becomes a
    # callable merely because the dispatcher can write it.
    assert adapter.declared_supported_case_ids == {
        "protected_overwrite",
        "workspace_boundary",
    }
    assert adapter.permission_requires_live_channel is False
    expected_caps = {"protected_overwrite": 0, "workspace_boundary": 0}
    assert {
        case.case_id: adapter.live_call_cap(case) for case in PERMISSION_CASE_SPECS
    } == {
        case.case_id: expected_caps.get(case.case_id, 0)
        for case in PERMISSION_CASE_SPECS
    }

    evidence_path = context.artifact_root / catalog.raw_catalog_ref
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["snapshot"] == context.snapshot.to_dict()
    assert payload["loader_id"] == loader_id
    assert payload["ordinary_native_tool_schemas"] == []
    assert runtime_phrase in payload["observation"]
    assert "file_only" not in json.dumps(payload)


def test_unconfigured_unsupported_adapter_keeps_catalog_not_evaluated(tmp_path: Path) -> None:
    adapter = UnsupportedPermissionPolicyAdapter(
        name="no-catalog",
        kind=RuntimeKind.DETERMINISTIC,
        missing_requirement="native_permission_decision_route_unavailable",
    )
    context = _context(tmp_path, "unconfigured")

    assert adapter.collect_native_tool_catalog(context) is None
    assert adapter.native_tool_catalog_reason(context.snapshot) == "native_tool_catalog_unavailable"


@pytest.mark.parametrize("harness", (MinimalHarness(), LLMHarness()))
def test_text_empty_catalog_cache_is_local_to_the_requested_evidence_context(
    tmp_path: Path, harness: MinimalHarness | LLMHarness
) -> None:
    context = _context(tmp_path, harness.name)
    adapter = harness.permission_policy_adapter()
    first = adapter.collect_native_tool_catalog(context)
    assert first is not None

    second_context = replace(
        context,
        trial_root=context.artifact_root / "trials" / harness.name / "second",
        evidence_dir=context.artifact_root / "trials" / harness.name / "second" / "raw",
    )
    second = adapter.collect_native_tool_catalog(second_context)

    assert second is not None
    assert second is not first
    assert (second_context.artifact_root / second.raw_catalog_ref).is_file()
    assert (second_context.artifact_root / first.raw_catalog_ref).is_file()
    assert second.raw_catalog_ref.startswith(
        f"trials/{harness.name}/second/raw/"
    )
