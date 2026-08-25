"""Model-mediated notes/tools binding for the LLM harness."""

from __future__ import annotations

import json
from dataclasses import asdict

from proteus.adapters.llm import PHASES, SYSTEM, _parse_actions, _render_state
from proteus.adapters.minimal_safety import MinimalSafetyRuntime
from proteus.core.adapter import ActionEvent
from proteus.safety.runtime import NativeReceipt, RuntimeKind, SafetyEpisodeResult


class LlmSafetyRuntime(MinimalSafetyRuntime):
    """Reuse Minimal's file primitives and replace only episode mediation."""

    name = "llm"
    kind = RuntimeKind.MODEL_MEDIATED

    def __init__(self) -> None:
        # Minimal's shared file/effect primitives do not need the episode adapter.
        # Keeping it here would retain the trusted controller's credential-owning factory.
        super().__init__(harness=None)

    def run_safety_episode(self, prompts, context, channel) -> SafetyEpisodeResult:
        if channel is None:
            raise ValueError("LLM safety runtime requires a live model channel")
        harness_root = context.snapshot_root
        for subdir in ("notes", "tools"):
            (harness_root / subdir).mkdir(parents=True, exist_ok=True)
        trace_path = context.trial_root / "traces" / f"ep{context.episode:03d}.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        events: list[ActionEvent] = []
        receipts: list[NativeReceipt] = []
        provenance = []
        turn = 0
        error = ""
        with trace_path.open("w", encoding="utf-8") as sink:
            for phase in PHASES:
                user = (
                    f"Episode {context.episode}, phase: {phase}.\n\n"
                    f"{prompts[phase]}\n\nCurrent harness state:\n"
                    f"{_render_state(harness_root)}"
                )
                try:
                    response = channel.respond(input=user, instructions=SYSTEM)
                    if (
                        response.model != channel.model
                        or response.model != response.provenance.response_model
                        or channel.model != response.provenance.configured_model
                    ):
                        raise ValueError("live model provenance does not match the channel")
                except Exception as exc:  # noqa: BLE001 - terminal evidence records failure
                    error = f"{type(exc).__name__}: {exc}"
                    break
                provenance.append(response.provenance)
                turn += 1
                model_event = ActionEvent(
                    turn=turn,
                    phase=phase,
                    text=response.output_text[:500],
                )
                events.append(model_event)
                sink.write(json.dumps(asdict(model_event), sort_keys=True) + "\n")
                for action in _parse_actions(response.output_text):
                    tool = str(action.get("tool", ""))
                    name = "".join(
                        character
                        for character in str(action.get("name", "unnamed"))
                        if character.isalnum() or character in "-_"
                    )[:60] or "unnamed"
                    text = str(action.get("text", ""))
                    if tool == "write_note":
                        target = harness_root / "notes" / f"{name}.md"
                        surface = "notes"
                        params = {"state_id": name}
                    elif tool == "write_tool":
                        target = harness_root / "tools" / f"{name}.py"
                        surface = "tools"
                        params = {}
                    else:
                        continue
                    turn += 1
                    target.write_text(text, encoding="utf-8")
                    event = ActionEvent(
                        turn=turn,
                        phase=phase,
                        tool=tool,
                        surface=surface,
                        params=params,
                        text=name,
                    )
                    events.append(event)
                    sink.write(json.dumps(asdict(event), sort_keys=True) + "\n")
        trace_ref = (
            trace_path.relative_to(context.artifact_root).as_posix()
            if context.artifact_root is not None
            else f"trials/{context.trial_root.name}/traces/ep{context.episode:03d}.jsonl"
        )
        for event in events:
            if event.tool is None:
                continue
            receipts.append(
                NativeReceipt(
                    operation_id=f"llm-turn-{event.turn}",
                    proposed=True,
                    attempted=True,
                    completed=True,
                    result_delivered=True,
                    authorized=None,
                    evidence_refs=(trace_ref,),
                )
            )
        summary_refs = self._record(
            context,
            f"llm-episode-{context.episode}",
            {
                "operation_id": f"llm-episode-{context.episode}",
                "terminal": not error,
                "turns": turn,
                "error": error,
                "events": [asdict(event) for event in events],
                "model_provenance": [asdict(item) for item in provenance],
            },
        )
        return SafetyEpisodeResult(
            terminal=not error,
            events=tuple(events),
            receipts=tuple(receipts),
            model_provenance=tuple(provenance),
            evidence_refs=summary_refs + (trace_ref,),
            error=error,
        )
