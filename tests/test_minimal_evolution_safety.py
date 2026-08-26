from __future__ import annotations

from pathlib import Path

from proteus.adapters.minimal import MinimalHarness
from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import (
    EffectRequest,
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


def test_minimal_runtime_denies_protected_effect_and_commits_allowed_effect(
    tmp_path: Path,
) -> None:
    runtime = MinimalHarness().safety_runtime()
    context = _context(tmp_path)
    protected = EffectRequest(
        effect_id="protected-effect",
        operation="write_file",
        resource="protected/marker.txt",
        content="must-not-commit",
        externally_authorized=False,
    )
    allowed = EffectRequest(
        effect_id="allowed-effect",
        operation="write_file",
        resource="allowed/marker.txt",
        content="allowed-content",
        externally_authorized=True,
    )

    denied = runtime.invoke_effect(protected, context)
    completed = runtime.invoke_effect(allowed, context)

    assert denied.proposed and denied.authorized is False and not denied.completed
    assert completed.proposed and completed.attempted and completed.completed
    assert not runtime.effect_oracle(protected.effect_id, protected.content, context)
    assert runtime.effect_oracle(allowed.effect_id, allowed.content, context)
