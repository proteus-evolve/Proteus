"""Real notes/tools binding for Minimal's universal safety runtime."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from proteus.core.adapter import EpisodeSpec
from proteus.safety.plugins import CandidateSafetyContext
from proteus.safety.runtime import (
    MemoryFaultRequest,
    MemoryStateRequest,
    NativeReceipt,
    RuntimeKind,
    SafetyEpisodeResult,
)


class MinimalSafetyRuntime:
    """Bind generic primitives to Minimal's actual files, episode loop, and trace."""

    name = "minimal"
    kind = RuntimeKind.DETERMINISTIC

    def __init__(self, harness) -> None:
        self._harness = harness

    @staticmethod
    def _safe_name(value: str) -> str:
        return "".join(character if character.isalnum() or character in "-_" else "-"
                       for character in value)

    def _record(
        self, context: CandidateSafetyContext, operation_id: str, payload: dict
    ) -> tuple[str, ...]:
        context.evidence_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{self._safe_name(operation_id)}.json"
        path = context.evidence_dir / filename
        path.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        if context.artifact_root is not None:
            return (path.relative_to(context.artifact_root).as_posix(),)
        if context.evidence_dir.parent.name == "evidence":
            return (f"evidence/{context.evidence_dir.name}/{filename}",)
        return (f"evidence/{filename}",)

    @staticmethod
    def _memory_path(state_id: str, context: CandidateSafetyContext) -> Path:
        return context.snapshot_root / "notes" / f"{state_id}.md"

    def introduce_memory(
        self, request: MemoryStateRequest, context: CandidateSafetyContext
    ) -> NativeReceipt:
        operation_id = f"memory-introduce-{request.state_id}"
        path = self._memory_path(request.state_id, context)
        attempted = False
        completed = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            attempted = True
            path.write_text(request.body, encoding="utf-8")
            completed = True
        finally:
            refs = self._record(
                context,
                operation_id,
                {
                    "operation_id": operation_id,
                    "native_path": str(path.relative_to(context.snapshot_root)),
                    "unsafe": request.unsafe,
                    "attempted": attempted,
                    "completed": completed,
                    "result_delivered": True,
                },
            )
        return NativeReceipt(
            operation_id, True, attempted, completed, True, None, refs
        )

    def read_memory(
        self, state_id: str, context: CandidateSafetyContext
    ) -> NativeReceipt:
        operation_id = f"memory-read-{state_id}"
        path = self._memory_path(state_id, context)
        completed = path.is_file()
        if completed:
            path.read_text(encoding="utf-8")
        refs = self._record(
            context,
            operation_id,
            {
                "operation_id": operation_id,
                "native_path": str(path.relative_to(context.snapshot_root)),
                "attempted": True,
                "completed": completed,
                "result_delivered": True,
            },
        )
        return NativeReceipt(operation_id, True, True, completed, True, None, refs)

    def inject_memory_fault(
        self, request: MemoryFaultRequest, context: CandidateSafetyContext
    ) -> NativeReceipt:
        operation_id = f"memory-fault-{request.state_id}"
        supported = request.operation == "remove_qualified_state"
        path = self._memory_path(request.state_id, context)
        attempted = supported
        completed = False
        if supported and path.is_file():
            path.unlink()
            completed = not path.exists()
        refs = self._record(
            context,
            operation_id,
            {
                "operation_id": operation_id,
                "operation": request.operation,
                "native_path": str(path.relative_to(context.snapshot_root)),
                "attempted": attempted,
                "completed": completed,
                "result_delivered": True,
            },
        )
        return NativeReceipt(operation_id, True, attempted, completed, True, None, refs)

    def run_safety_episode(self, prompts, context, channel) -> SafetyEpisodeResult:
        if channel is not None:
            raise ValueError("Minimal is deterministic and does not accept a live channel")
        result = self._harness.run_episode(
            EpisodeSpec(
                root=context.trial_root,
                episode=context.episode,
                model="mock",
                phase_prompts=prompts,
                max_turns=20,
                seed=0,
                continuity_mode="none",
            )
        )
        events = tuple(self._harness.read_trace(context.trial_root, context.episode))
        operation_id = f"minimal-episode-{context.episode}"
        summary_refs = self._record(
            context,
            operation_id,
            {
                "operation_id": operation_id,
                "terminal": result.ok,
                "turns": result.turns,
                "error": result.error,
                "events": [asdict(event) for event in events],
            },
        )
        trace_path = context.trial_root / "traces" / f"ep{context.episode:03d}.jsonl"
        trace_ref = (
            trace_path.relative_to(context.artifact_root).as_posix()
            if context.artifact_root is not None
            else f"trials/{context.trial_root.name}/traces/ep{context.episode:03d}.jsonl"
        )
        receipts = tuple(
            NativeReceipt(
                operation_id=f"minimal-turn-{event.turn}",
                proposed=True,
                attempted=True,
                completed=True,
                result_delivered=True,
                authorized=None,
                evidence_refs=(trace_ref,),
            )
            for event in events
            if event.tool is not None
        )
        return SafetyEpisodeResult(
            terminal=result.ok,
            events=events,
            receipts=receipts,
            model_provenance=(),
            evidence_refs=summary_refs + (trace_ref,),
            error=result.error,
        )

    def memory_oracle(
        self, state_id: str, expected_body: str, context: CandidateSafetyContext
    ) -> bool:
        path = self._memory_path(state_id, context)
        return path.is_file() and path.read_text(encoding="utf-8") == expected_body
