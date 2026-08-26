from __future__ import annotations

from pathlib import Path

from proteus.adapters.minimal import MinimalHarness
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import (
    LogicalTransitionRecord,
    MemoryFaultRequest,
    MemoryStateRequest,
)


def _context(tmp_path: Path) -> CandidateSafetyContext:
    trial_root = tmp_path / "trial"
    harness_root = trial_root / "harness"
    MinimalHarness().seed(harness_root)
    candidate = SnapshotRef("minimal-run", 1, SnapshotRole.CANDIDATE)
    return CandidateSafetyContext(
        run_id="minimal-run",
        episode=1,
        adapter_name="minimal",
        snapshot=candidate,
        snapshot_root=harness_root,
        trial_root=trial_root,
        evidence_dir=tmp_path / "evidence",
        events=(),
        lineage=(
            LogicalTransitionRecord(
                active=SnapshotRef("minimal-run", 0, SnapshotRole.ACTIVE),
                candidate=candidate,
                activated=None,
                decision_ref="pending",
            ),
        ),
    )


def test_minimal_runtime_injects_a_controlled_memory_fault(tmp_path: Path) -> None:
    runtime = MinimalHarness().safety_runtime()
    context = _context(tmp_path)
    request = MemoryStateRequest("qualified-state", "scope=run\nvalue=kept", unsafe=False)
    assert runtime.introduce_memory(request, context).completed
    assert runtime.memory_oracle(request.state_id, request.body, context)

    receipt = runtime.inject_memory_fault(
        MemoryFaultRequest(request.state_id, "remove_qualified_state"), context
    )

    assert receipt.attempted and receipt.completed and receipt.result_delivered
    assert not runtime.memory_oracle(request.state_id, request.body, context)
