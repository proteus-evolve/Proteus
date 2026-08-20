"""A hidden, source-level capability rubric for evolving audio input into DSH.

DeepSeek Harness rc.8 has a complete image path but no first-class audio path.  This
evaluator gives an evolving harness a dense signal across the integration boundaries an
audio implementation has to cross.  It deliberately does not prescribe concrete module
names or a transcription vendor; the final release gate remains the upstream build and
end-to-end audio fixtures described in ``docs/DSH_AUDIO_EVOLUTION.md``.

The evaluator lives outside the evolving ``harness/`` tree.  The subject can receive its
score, but cannot edit the rubric itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

from proteus.core.adapter import ActionEvent
from proteus.core.goal import EvalResult, GoalContext

NAME = "dsh-audio-capability"

GOAL_TEXT = """\
Evolve your DeepSeek Harness source from the pinned rc.8 baseline so audio becomes a
first-class user input. Users should be able to attach common audio files through the web
composer and ACP, have the harness validate and persist them safely, transcribe them
through a configurable provider-neutral seam, see the audio and transcript in history,
and use that grounded transcript in ordinary prompts and commands such as /goal and /plan.
Preserve the existing image path. Do not invent a native DeepSeek wire field: the public
DeepSeek Chat Completions API does not currently document native audio input. Add focused
tests and keep the monorepo buildable.
"""


def _source_files(root: Path, parts: Sequence[str]) -> Iterable[Path]:
    for part in parts:
        base = root / part
        if base.is_file():
            yield base
        elif base.is_dir():
            yield from (p for p in base.rglob("*") if p.suffix in {".ts", ".tsx", ".md"})


def _joined(root: Path, *parts: str) -> str:
    chunks = []
    for path in _source_files(root, parts):
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def capability_gates(source_root: Path) -> dict[str, bool]:
    """Return the six independently useful audio-integration gates.

    These are intentionally coarse architectural checks.  They supply gradient during an
    evolution run; they are not a substitute for compiling or playing the held-out audio
    fixtures at the final release gate.
    """
    source_root = Path(source_root)
    core = _joined(source_root, "packages/llm/llm/src/types.ts")
    attachments = _joined(source_root, "packages/attachment")
    acp = _joined(source_root, "packages/acp/acp/src")
    client = _joined(
        source_root,
        "packages/client/ui-attachment",
        "packages/client/ui-conversation",
    )
    packages = _joined(source_root, "packages")

    audio_case = re.search(r"case\s+['\"]audio['\"]", acp, re.I) is not None
    acp_rejects_audio = "audio prompt content is not supported" in acp.lower()
    test_areas = set()
    for path in _source_files(source_root, ("packages", "apps")):
        if not any(part in {"test", "tests"} for part in path.parts) \
                and not path.name.endswith((".spec.ts", ".test.ts")):
            continue
        try:
            test_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # rc.8 already has negative protocol fixtures proving that audio is rejected.
        # Count only tests that exercise an affirmative harness-level audio concept.
        if not re.search(
            r"(AudioBlock|AudioAttachment|saveAudio|readAudio|transcrib|AudioPlayer|<audio)",
            test_text,
            re.I,
        ):
            continue
        text = path.as_posix()
        for area in ("attachment", "acp", "client", "llm", "command", "mcp"):
            if area in text:
                test_areas.add(area)

    return {
        "core block": bool(re.search(r"interface\s+AudioBlock\b", core)
                           and re.search(r"['\"]audio['\"]\s*:\s*AudioBlock", core)),
        "durable storage": all(token.lower() in attachments.lower() for token in (
            "AudioAttachmentRef", "AudioMediaType", "saveAudio", "readAudio")),
        "ACP admission": bool(audio_case and not acp_rejects_audio
                              and re.search(r"(save|admit|persist|transcrib).*audio", acp, re.I | re.S)),
        "web composer": bool(re.search(r"audio/", client, re.I)
                             and re.search(r"(<audio|AudioPlayer|audio\s+attachment)", client, re.I)
                             and re.search(r"(composer|draft|drop|attach)", client, re.I)),
        "transcription seam": bool(re.search(r"transcrib", packages, re.I)
                                   and re.search(
                                       r"(provider|adapter|backend|service|interface).*transcrib|"
                                       r"transcrib.*(provider|adapter|backend|service|interface)",
                                       packages, re.I | re.S)),
        "cross-layer tests": len(test_areas) >= 4,
    }


def evaluate_audio_capability(
    trace: Sequence[ActionEvent], ctx: GoalContext,
) -> EvalResult:
    """Score the current DSH source tree from 0 to 1 across the six gates."""
    del trace  # source capability, not activity, is the measured outcome
    gates = capability_gates(Path(ctx.harness_root) / "src")
    passed = sum(gates.values())
    detail = "; ".join(f"{name}={'yes' if ok else 'no'}" for name, ok in gates.items())
    return EvalResult(
        name=NAME,
        score=passed / len(gates),
        passed=passed == len(gates),
        detail=f"{passed}/{len(gates)} gates; {detail}",
    )
