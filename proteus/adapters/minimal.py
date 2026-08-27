"""A minimal, dependency-free reference harness — proof that Proteus is harness-agnostic.

This harness is deliberately tiny: two surfaces (`notes`, `tools`), a four-phase loop, and
a pluggable policy. It exists to (a) let the whole framework run end-to-end with no API key
and no Docker, so tests and demos work offline, and (b) demonstrate that a harness with
*its own* surface set plugs in with no change to the framework or the measurement layer.

The default `MockPolicy` is deterministic and biases its writes toward whichever surface the
installed disposition names, so even offline a `review_notes` run diverges from `neutral` in
a way the distance instrument can measure. `proteus.adapters.llm.LLMHarness` is the same
harness driven by a live model instead of the mock policy.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from proteus.core.adapter import ActionEvent, EpisodeResult, EpisodeSpec, Surface
from proteus.core.budget import PHASES, budget_plan, phase_prompt
from proteus.core.disposition import Disposition

# A policy maps (phase, prompt, episode, rng) -> list of (tool, surface, text) actions.
Action = Tuple[str, Optional[str], str]
Policy = Callable[[str, str, int, random.Random], list[Action]]


def mock_policy(phase: str, prompt: str, episode: int, rng: random.Random) -> list[Action]:
    """Deterministic offline policy. Writes to the surface the prompt leans on.

    It reads the disposition's leaning straight from the phase prompt text (the prompt is
    all the real agent would see too), so a notes-disposition produces notes and a
    tools-disposition produces tools — a measurable, reproducible effect without any model.
    """
    low = prompt.lower()
    leans_notes = "notes" in low
    leans_tools = "tools" in low
    acts: list[Action] = []
    if phase == "observe":
        acts.append(("read_state", None, "surveyed the harness"))
    elif phase == "act":
        # baseline: occasionally write a note; disposition tilts the surface + rate.
        if leans_tools or (not leans_notes and rng.random() < 0.3):
            acts.append(("write_tool", "tools", f"tool_e{episode}"))
        if leans_notes or rng.random() < 0.5:
            acts.append(("write_note", "notes", f"note_e{episode}_{rng.randint(0,3)}"))
    elif phase == "reflect" and leans_notes and rng.random() < 0.6:
        acts.append(("write_note", "notes", f"reflection_e{episode}"))
    return acts


class MinimalHarness:
    """A `HarnessAdapter` for the minimal reference harness."""

    name = "minimal"
    continuity_mode = "none"
    disposition_in_files = False   # the perturbation reaches this harness via prompts

    def __init__(self, policy: Policy = mock_policy) -> None:
        self.policy = policy

    def surfaces(self) -> Sequence[Surface]:
        return [
            Surface("notes", "notes", unit="file", write_tools=frozenset({"write_note"})),
            Surface("tools", "tools", unit="file", write_tools=frozenset({"write_tool"}),
                    is_code=True),
        ]

    def required_edit_tools(self) -> frozenset[str]:
        return frozenset({"write_note", "write_tool"})

    def seed(self, harness_root: Path, rng_seed: int = 0) -> None:
        for sub in ("notes", "tools"):
            (harness_root / sub).mkdir(parents=True, exist_ok=True)
        (harness_root / "STATE.md").write_text("# minimal harness\nepisode 0\n")

    def install_disposition(self, harness_root: Path, disposition: Disposition) -> None:
        (harness_root / ".disposition.json").write_text(json.dumps({
            "label": disposition.label,
            "config": dict(disposition.config),
            "empty": disposition.is_empty,
        }))

    def run_episode(self, spec: EpisodeSpec) -> EpisodeResult:
        harness = spec.root / "harness"
        for sub in ("notes", "tools"):
            # git snapshots do not track empty directories, so a restore after a rejected
            # episode may drop them; an episode must tolerate waking up without them
            (harness / sub).mkdir(exist_ok=True)
        rng = random.Random(f"{spec.seed}:{spec.root.name}:{spec.episode}")
        trace_path = spec.root / "traces" / f"ep{spec.episode:03d}.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        turn = 0
        writes = {"notes": 0, "tools": 0}
        phase_counts = {phase: 0 for phase in PHASES}
        capped = False
        plan = budget_plan(spec)
        with trace_path.open("w", encoding="utf-8") as sink:
            for phase in PHASES:
                if plan.enabled and turn >= plan.hard_limit:
                    capped = True
                if capped:
                    break
                stop_at = plan.stop_at(phase, turn)
                prompt = phase_prompt(spec, phase, turn)
                for tool, surface, text in self.policy(phase, prompt, spec.episode, rng):
                    if plan.enabled and turn >= stop_at:
                        capped = turn >= plan.hard_limit
                        break
                    turn += 1
                    phase_counts[phase] += 1
                    if tool == "write_note":
                        (harness / "notes" / f"{text}.md").write_text(
                            f"episode {spec.episode}: {text}\n")
                        writes["notes"] += 1
                    elif tool == "write_tool":
                        (harness / "tools" / f"{text}.py").write_text(
                            f"# episode {spec.episode}\ndef {text}():\n    return {spec.episode}\n")
                        writes["tools"] += 1
                    sink.write(json.dumps({
                        "turn": turn, "phase": phase, "tool": tool,
                        "surface": surface, "text": text,
                    }) + "\n")
        counters = {"writes": writes, "turn_capped": capped}
        counters.update({f"phase_{phase}_turns": count
                         for phase, count in phase_counts.items()})
        return EpisodeResult(episode=spec.episode, ok=True, turns=turn, counters=counters)

    def read_trace(self, root: Path, episode: int) -> Sequence[ActionEvent]:
        path = root / "traces" / f"ep{episode:03d}.jsonl"
        out: list[ActionEvent] = []
        if not path.exists():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            out.append(ActionEvent(turn=d["turn"], phase=d["phase"], tool=d.get("tool"),
                                   surface=d.get("surface"), text=d.get("text", "")))
        return out

    def disposition_fingerprint(self, harness_root: Path) -> str:
        import hashlib
        p = harness_root / ".disposition.json"
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""
