#!/usr/bin/env python3
"""Render the 30-second Proteus v0.1.0 launch film with original jazz piano.

The visual language deliberately borrows the live trajectory, breathing episode dot,
and expanded episode dome from the Proteus homepage.  Type is kept large and bold for
social timelines; no essential copy is set below 27 px at 1080p.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import render_features_video as ui


W, H = 1920, 1080
FPS, DURATION, SAMPLE_RATE = 30, 30, 44_100
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "proteus-v0.1.0-dsh-teaser-30s.mp4"

BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
SERIF_BOLD = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"
SERIF_ITALIC = "/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf"
MONO_BOLD = "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"

F = {
    "micro": ImageFont.truetype(MONO_BOLD, 27),
    "small": ImageFont.truetype(BOLD, 34),
    "body": ImageFont.truetype(BOLD, 46),
    "sub": ImageFont.truetype(SERIF_BOLD, 62),
    "title": ImageFont.truetype(SERIF_BOLD, 92),
    "hero": ImageFont.truetype(SERIF_BOLD, 124),
    "hero_i": ImageFont.truetype(SERIF_ITALIC, 124),
    "number": ImageFont.truetype(BOLD, 154),
}


def _case() -> dict:
    raw = (ROOT / "web/static/assets/case-data.js").read_text(encoding="utf-8").strip()
    return json.loads(raw.removeprefix("window.CASE = ").removesuffix(";"))


CASE = _case()
EPISODES = CASE["episodes"]
SURFACE = {"memory": ui.BLUE, "skills": ui.LEAF, "tools": ui.ORANGE}


def dominant(index: int) -> str:
    current = EPISODES[index]["units"]
    previous = EPISODES[index - 1]["units"] if index else {k: 0 for k in current}
    gains = {key: current[key] - previous.get(key, 0) for key in current}
    name, gain = max(gains.items(), key=lambda item: item[1])
    return SURFACE[name] if gain > 0 else ui.DIM


def alpha(t: float, start: float, end: float, fade: float = 0.48) -> float:
    return min(ui.smooth((t - start) / fade), ui.smooth((end - t) / fade))


def text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], value: str,
         face: ImageFont.FreeTypeFont, fill: str = ui.INK, anchor: str = "la") -> None:
    draw.text(xy, value, font=face, fill=fill, anchor=anchor, stroke_width=0)


def base_frame(t: float) -> Image.Image:
    image = Image.new("RGBA", (W, H), ui.BG)
    draw = ImageDraw.Draw(image)
    for x in range(0, W, 120):
        draw.line((x, 0, x, H), fill="#E8E1D2", width=1)
    for y in range(0, H, 120):
        draw.line((0, y, W, y), fill="#E8E1D2", width=1)
    text(draw, (76, 58), "PROTEUS  /  V0.1.0", F["micro"], ui.INK, "lm")
    text(draw, (W - 76, 58), "HARNESS SELF-EVOLUTION", F["micro"], ui.GREEN, "rm")
    draw.line((76, 96, W - 76, 96), fill=ui.RULE, width=3)
    draw.line((76, H - 65, W - 76, H - 65), fill=ui.RULE, width=3)
    draw.rectangle((76, H - 68, 76 + (W - 152) * t / DURATION, H - 63), fill=ui.GREEN)
    return image


def orb(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float,
        progress: float, *, color: str = ui.GREEN, label: str = "") -> None:
    pulse = 1 + 0.022 * math.sin(progress * math.tau * 2)
    r = radius * pulse
    for extra, opacity in ((36, 18), (18, 30)):
        draw.ellipse((cx - r - extra, cy - r - extra, cx + r + extra, cy + r + extra),
                     fill=ui.mix_hex(ui.BG, color, opacity / 255))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    draw.ellipse((cx - r * .82, cy - r * .82, cx + r * .82, cy + r * .82),
                 outline=ui.mix_hex(color, "#FFFFFF", .26), width=3)
    if label:
        text(draw, (cx, cy), label, F["micro"], "#FFFFFF", "mm")


def trajectory(draw: ImageDraw.ImageDraw, t: float, box: tuple[int, int, int, int],
               start_episode: int = 8, count: int = 12) -> None:
    x1, y1, x2, y2 = box
    indices = list(range(start_episode, min(start_episode + count, len(EPISODES))))
    calls = [len(EPISODES[i]["steps"]) for i in indices]
    low, high = min(calls), max(calls)
    points = []
    for j, (idx, value) in enumerate(zip(indices, calls)):
        x = x1 + j * (x2 - x1) / max(1, len(indices) - 1)
        y = y2 - (value - low) / max(1, high - low) * (y2 - y1)
        points.append((x, y, idx))
    visible = max(1, min(len(points), int(ui.ease(t) * len(points)) + 1))
    line_points = [(x, y) for x, y, _ in points[:visible]]
    if len(line_points) > 1:
        draw.line(line_points, fill=ui.SOFT, width=5, joint="curve")
    for x, y, idx in points[:visible]:
        r = 14 if idx == points[visible - 1][2] else 9
        draw.ellipse((x - r, y - r, x + r, y + r), fill=dominant(idx))
    x, y, idx = points[visible - 1]
    halo = 23 + 8 * math.sin(t * math.tau * 2)
    draw.ellipse((x - halo, y - halo, x + halo, y + halo), outline=ui.GREEN, width=4)


def scene_intro(t: float) -> Image.Image:
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    p = ui.ease(t / 1.0)
    orb(draw, 1535, 550, 250, t / 5, label="EPISODE 01")
    ui.logo_mark(draw, 130, 206, 0.85)
    text(draw, (130, 440 + 35 * (1 - p)), "Your agent", F["hero"], ui.INK, "lm")
    text(draw, (130, 575 + 35 * (1 - p)), "evolves.", F["hero_i"], ui.GREEN, "lm")
    text(draw, (130, 705), "Now its harness can too.", F["sub"], ui.INK, "lm")
    text(draw, (134, 815), "PROTEUS V0.1.0 IS LIVE", F["body"], ui.GREEN, "lm")
    text(draw, (134, 875), "Open source · reproducible · episode by episode", F["small"], ui.SOFT, "lm")
    return image


def scene_choose(t: float) -> Image.Image:
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    local = t - 4.5
    text(draw, (W / 2, 190), "Choose the system that evolves.", F["title"], ui.INK, "mm")
    orb(draw, 960, 560, 175, local / 5, label="EVOLVE")
    cards = [
        (170, "HARNESS", "DSH", ui.GREEN),
        (1250, "MODEL", "DEEPSEEK", ui.BLUE),
    ]
    for x, cap, value, color in cards:
        ui.rounded(draw, (x, 365, x + 500, 750), 28, ui.PANEL, color, 4)
        text(draw, (x + 38, 422), cap, F["small"], color, "lm")
        text(draw, (x + 250, 564), value, F["sub"], ui.INK, "mm")
        text(draw, (x + 250, 657), "OPEN SOURCE", F["micro"], ui.SOFT, "mm")
    text(draw, (W / 2, 875), "OR BRING ANY CUSTOM HARNESS", F["body"], ui.GREEN, "mm")
    return image


def scene_goals(t: float) -> Image.Image:
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    local = t - 9.2
    text(draw, (120, 195), "Describe what it should become.", F["title"], ui.INK, "lm")
    text(draw, (125, 295), "Natural language. One goal or many.", F["body"], ui.GREEN, "lm")
    orb(draw, 1500, 570, 235, local / 5, label="YOUR GOAL")
    goals = [
        (125, 415, "MORE RELIABLE", ui.GREEN),
        (125, 565, "BETTER ON A BENCHMARK", ui.BLUE),
        (125, 715, "NEW MULTIMODAL ABILITIES", ui.ORANGE),
    ]
    for i, (x, y, value, color) in enumerate(goals):
        show = ui.ease((local - i * .28) / .62)
        ui.rounded(draw, (x + 36 * (1 - show), y, 1110, y + 105), 18,
                   ui.PANEL, ui.mix_hex(ui.RULE, color, show), 4)
        text(draw, (x + 42, y + 54), value, F["body"], color, "lm")
    return image


def scene_evolve(t: float) -> Image.Image:
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    local = t - 13.9
    p = ui.clamp(local / 6.4)
    ep = 15 + min(7, int(p * 8))
    orb(draw, 445, 570, 315, local / 6.4)
    text(draw, (445, 405), "LIVE EVOLUTION", F["micro"], "#FFFFFF", "mm")
    text(draw, (445, 555), f"{ep:02d}", F["number"], "#FFFFFF", "mm")
    text(draw, (445, 675), "EPISODE", F["small"], "#FFFFFF", "mm")
    text(draw, (900, 205), "Watch every episode evolve.", F["title"], ui.INK, "lm")
    trajectory(draw, p, (920, 360, 1770, 610), start_episode=8, count=12)
    phase = ["OBSERVE", "PROPOSE", "ACT", "REFLECT"][int(local * 1.2) % 4]
    details = [
        ("PHASE", phase, ui.GREEN),
        ("TRACE", "READ · EDIT · TEST", ui.INK),
        ("BOUNDARY", "GIT SNAPSHOT", ui.BLUE),
        ("PUBLIC", "COMPLETED EPISODES ONLY", ui.ORANGE),
    ]
    for i, (cap, value, color) in enumerate(details):
        y = 675 + i * 65
        text(draw, (920, y), cap, F["micro"], ui.SOFT, "lm")
        text(draw, (1175, y), value, F["small"], color, "lm")
    return image


def scene_measure(t: float) -> Image.Image:
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    local = t - 19.7
    text(draw, (W / 2, 175), "Measure. Snapshot. Compare.", F["title"], ui.INK, "mm")
    snapshots = [
        (390, "EP 04", "0.33", ui.BLUE),
        (960, "EP 08", "0.67", ui.ORANGE),
        (1530, "EP 12", "1.00", ui.GREEN),
    ]
    for i, (cx, ep, score, color) in enumerate(snapshots):
        show = ui.ease((local - i * .35) / .75)
        radius = 190 * show
        if radius < 2:
            continue
        orb(draw, cx, 565, radius, local / 5.5, color=color)
        text(draw, (cx, 525), ep, F["small"], "#FFFFFF", "mm")
        text(draw, (cx, 620), score, F["sub"], "#FFFFFF", "mm")
        if i:
            draw.line((snapshots[i - 1][0] + 205, 565, cx - 205, 565), fill=ui.RULE, width=5)
    text(draw, (W / 2, 860), "KEEP EVERY VERSION · REPLAY EVERY DECISION", F["body"], ui.GREEN, "mm")
    return image


def scene_teaser(t: float) -> Image.Image:
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    local = t - 24.5
    p = ui.ease(local / .9)
    # Expanded episode dome: the homepage dot grows until it becomes the detail view.
    radius = 460 * p
    draw.ellipse((-radius * 1.05, 540 - radius, radius * .95, 540 + radius), fill=ui.GREEN)
    if p > .25:
        text(draw, (100, 425), "NEXT", F["micro"], "#FFFFFF", "lm")
        text(draw, (100, 515), "DSH", F["number"], "#FFFFFF", "lm")
        text(draw, (100, 625), "AUDIO", F["body"], "#FFFFFF", "lm")
    text(draw, (650, 250), "DeepSeek Harness", F["title"], ui.INK, "lm")
    text(draw, (650, 380), "just learned to see.", F["sub"], ui.INK, "lm")
    text(draw, (650, 545), "Can it evolve", F["hero"], ui.GREEN, "lm")
    text(draw, (650, 680), "itself to hear?", F["hero_i"], ui.GREEN, "lm")
    text(draw, (655, 810), "FULL TRACE · EPISODE BY EPISODE · LIVE SOON", F["small"], ui.INK, "lm")
    text(draw, (655, 875), "PROTEUS-EVOLVE.GITHUB.IO", F["body"], ui.GREEN, "lm")
    return image


SCENES = [
    (0.0, 5.0, scene_intro),
    (4.5, 9.8, scene_choose),
    (9.2, 14.6, scene_goals),
    (13.9, 20.6, scene_evolve),
    (19.7, 25.3, scene_measure),
    (24.5, 30.01, scene_teaser),
]


def render_frame(frame: int) -> Image.Image:
    t = frame / FPS
    canvas = base_frame(t)
    for start, end, renderer in SCENES:
        if start <= t <= end:
            ui.composite(canvas, renderer(t), alpha(t, start, end))
    return canvas.convert("RGB")


def midi(note: int) -> float:
    return 440 * 2 ** ((note - 69) / 12)


def original_jazz_piano(path: Path) -> None:
    """Synthesize an original 12-bar, light-jazz piano cue (no sampled material)."""
    total = SAMPLE_RATE * DURATION
    track = np.zeros((total, 2), dtype=np.float64)
    rng = np.random.default_rng(14)

    def add_note(start: float, duration: float, note: int, velocity: float, pan: float = 0) -> None:
        begin = max(0, int(start * SAMPLE_RATE))
        length = min(total - begin, int(duration * SAMPLE_RATE))
        if length <= 0:
            return
        tt = np.arange(length, dtype=np.float64) / SAMPLE_RATE
        freq = midi(note)
        attack = 1 - np.exp(-tt * 85)
        decay = np.exp(-tt * (2.2 + 1.2 / max(duration, .2)))
        tone = np.zeros(length)
        for harmonic, weight in ((1, 1.0), (2, .42), (3, .20), (4, .10), (6, .045)):
            phase = rng.uniform(0, math.tau)
            tone += weight * np.sin(math.tau * freq * harmonic * tt + phase)
        hammer = rng.normal(0, .06, length) * np.exp(-tt * 55)
        signal = (tone + hammer) * attack * decay * velocity * .14
        left = math.sqrt((1 - pan) / 2)
        right = math.sqrt((1 + pan) / 2)
        track[begin:begin + length, 0] += signal * left
        track[begin:begin + length, 1] += signal * right

    beat, bar = 60 / 96, 4 * (60 / 96)
    progression = [
        ([50, 57, 60, 64, 65], 38), ([43, 53, 57, 59, 64], 43),
        ([48, 55, 59, 62, 64], 36), ([45, 55, 58, 61, 65], 45),
        ([50, 57, 60, 64, 65], 38), ([43, 53, 57, 59, 64], 43),
        ([52, 59, 62, 67], 40), ([45, 55, 58, 61, 65], 45),
        ([50, 57, 60, 64, 65], 38), ([43, 53, 57, 59, 64], 43),
        ([48, 55, 59, 62, 64], 36), ([48, 55, 57, 62, 64], 36),
    ]
    melody = [74, 76, 77, 81, 79, 76, 74, 72, 71, 74, 76, 79,
              81, 79, 76, 74, 72, 71, 69, 67, 69, 71, 74, 72]
    melody_step = beat / 2
    for bar_index, (chord, bass) in enumerate(progression):
        start = bar_index * bar
        add_note(start, beat * 1.7, bass, .82, -.28)
        add_note(start + beat * 2, beat * 1.4, bass + 7, .64, -.22)
        for offset, strength in ((0, .68), (beat * 1.5, .48), (beat * 2.75, .56)):
            for j, note in enumerate(chord):
                add_note(start + offset + j * .008, beat * 1.35, note, strength, -.08 + j * .04)
        # A spare, swung melodic phrase leaves breathing room for the on-screen copy.
        if bar_index % 2 == 0:
            phrase = melody[(bar_index * 2) % len(melody):]
            for step in range(4):
                note = phrase[step % len(phrase)]
                swing = .08 if step % 2 else 0
                add_note(start + beat * (step + .35) + swing, melody_step * 1.45,
                         note, .52, .30)

    # Small-room reflections and a gentle master envelope.
    dry = track.copy()
    for delay, gain in ((.075, .17), (.145, .11), (.235, .07)):
        samples = int(delay * SAMPLE_RATE)
        track[samples:] += dry[:-samples] * gain
    fade = np.ones(total)
    fade_len = SAMPLE_RATE
    fade[:fade_len] = np.linspace(0, 1, fade_len)
    fade[-fade_len:] = np.linspace(1, 0, fade_len)
    track *= fade[:, None]
    peak = np.max(np.abs(track)) or 1
    track = np.tanh(track / peak * 1.25) * .78
    pcm = (track * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


def main() -> None:
    temporary = Path(tempfile.mkdtemp(prefix="proteus-release-30s-"))
    silent = temporary / "silent.mp4"
    music = temporary / "original-jazz-piano.wav"
    encoder = subprocess.Popen([
        "ffmpeg", "-y", "-loglevel", "warning", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264",
        "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags",
        "+faststart", str(silent),
    ], stdin=subprocess.PIPE)
    try:
        assert encoder.stdin is not None
        for frame in range(FPS * DURATION):
            encoder.stdin.write(render_frame(frame).tobytes())
            if frame % 150 == 0:
                print(f"rendered {frame:03d}/{FPS * DURATION}", flush=True)
        encoder.stdin.close()
        if encoder.wait() != 0:
            raise RuntimeError("video encoder failed")
        original_jazz_piano(music)
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "warning", "-i", str(silent), "-i", str(music),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
            "-movflags", "+faststart", str(OUT),
        ], check=True)
    finally:
        shutil.rmtree(temporary)
    print(OUT)


if __name__ == "__main__":
    main()
