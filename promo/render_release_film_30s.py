#!/usr/bin/env python3
"""Render the continuous-orb Proteus v0.1.0 release film.

Each feature is one expanded evolution node. The node contracts back into the trace,
the connecting edge grows, and the next feature node expands. At the end the camera
stays wide and the accumulated trace resolves into the Proteus mark.
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
OUT = HERE / "proteus-v0.1.0-release-30s.mp4"

BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
SERIF_BOLD = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"
MONO_BOLD = "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"
F = {
    "micro": ImageFont.truetype(MONO_BOLD, 27),
    "small": ImageFont.truetype(BOLD, 34),
    "body": ImageFont.truetype(BOLD, 46),
    "sub": ImageFont.truetype(SERIF_BOLD, 62),
    "title": ImageFont.truetype(SERIF_BOLD, 86),
    "hero": ImageFont.truetype(SERIF_BOLD, 112),
    "number": ImageFont.truetype(BOLD, 142),
}

FEATURES = [
    {"cap": "01 / PLUG IN", "title": "ANY HARNESS", "color": ui.GREEN},
    {"cap": "02 / EVOLVE", "title": "SELF-EVOLVE", "color": ui.BLUE},
    {"cap": "03 / SEE", "title": "VISUALIZE", "color": ui.ORANGE},
    {"cap": "04 / UNDERSTAND", "title": "ANALYZE", "color": ui.VIOLET},
]
FEATURE_NODES = (0, 1, 2, 4)
LOGO_POINTS = ((0, 0), (84, 1), (147, 36), (0, 72), (156, 102),
               (43, 111), (96, 139), (0, 149), (0, 224))
LOGO_EDGES = ((0, 1), (0, 3), (1, 2), (1, 5), (2, 4), (2, 5), (3, 5),
              (3, 7), (4, 6), (5, 6), (5, 7), (6, 7), (7, 8))
LOGO_ORIGIN, LOGO_SCALE = (205, 235), 2.45
FINAL_POINTS = tuple((LOGO_ORIGIN[0] + x * LOGO_SCALE,
                      LOGO_ORIGIN[1] + y * LOGO_SCALE) for x, y in LOGO_POINTS)
CENTER = (960, 555)


def _case() -> dict:
    raw = (ROOT / "web/static/assets/case-data.js").read_text(encoding="utf-8").strip()
    return json.loads(raw.removeprefix("window.CASE = ").removesuffix(";"))


EPISODES = _case()["episodes"]


def text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], value: str,
         face: ImageFont.FreeTypeFont, fill: str = ui.INK, anchor: str = "la") -> None:
    draw.text(xy, value, font=face, fill=fill, anchor=anchor)


def lerp(a: float, b: float, amount: float) -> float:
    return a + (b - a) * amount


def point_lerp(a: tuple[float, float], b: tuple[float, float], amount: float) -> tuple[float, float]:
    return lerp(a[0], b[0], amount), lerp(a[1], b[1], amount)


def paper(t: float, label: str) -> Image.Image:
    image = Image.new("RGBA", (W, H), ui.BG)
    draw = ImageDraw.Draw(image)
    for x in range(0, W, 120):
        draw.line((x, 0, x, H), fill="#E8E1D2", width=1)
    for y in range(0, H, 120):
        draw.line((0, y, W, y), fill="#E8E1D2", width=1)
    text(draw, (76, 58), "PROTEUS  /  V0.1.0", F["micro"], ui.INK, "lm")
    text(draw, (W - 76, 58), label, F["micro"], ui.GREEN, "rm")
    draw.line((76, 96, W - 76, 96), fill=ui.RULE, width=3)
    draw.line((76, H - 65, W - 76, H - 65), fill=ui.RULE, width=3)
    draw.rectangle((76, H - 68, 76 + (W - 152) * t / DURATION, H - 63), fill=ui.GREEN)
    return image


def node(draw: ImageDraw.ImageDraw, position: tuple[float, float], radius: float,
         color: str, pulse: float = 0) -> None:
    cx, cy = position
    r = radius * (1 + .018 * math.sin(pulse * math.tau))
    if r > 60:
        for extra, mix in ((34, .06), (17, .11)):
            draw.ellipse((cx - r - extra, cy - r - extra, cx + r + extra, cy + r + extra),
                         fill=ui.mix_hex(ui.BG, color, mix))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    if r > 80:
        draw.ellipse((cx - r * .84, cy - r * .84, cx + r * .84, cy + r * .84),
                     outline=ui.mix_hex(color, "#FFFFFF", .30), width=3)


def edge(draw: ImageDraw.ImageDraw, start: tuple[float, float], end: tuple[float, float],
         progress: float, color: str = ui.SOFT, width: int = 6) -> None:
    progress = ui.smooth(progress)
    target = point_lerp(start, end, progress)
    draw.line((*start, *target), fill=ui.mix_hex(ui.BG, color, .85), width=width)


def white(color: str, opacity: float) -> str:
    return ui.mix_hex(color, "#FFFFFF", ui.clamp(opacity))


def feature_content(draw: ImageDraw.ImageDraw, index: int, position: tuple[float, float],
                    opacity: float, progress: float) -> None:
    cx, cy = position
    feature = FEATURES[index]
    color = feature["color"]
    bright, soft = white(color, opacity), white(color, opacity * .72)
    text(draw, (cx, cy - 205), feature["cap"], F["micro"], soft, "mm")
    text(draw, (cx, cy - 110), feature["title"], F["title"], bright, "mm")

    if index == 0:
        labels = ("DSH", "PI", "CUSTOM")
        widths = (142, 120, 210)
        total = sum(widths) + 32 * 2
        x = cx - total / 2
        for label, width in zip(labels, widths):
            fill = ui.mix_hex(color, "#FFFFFF", .10 * opacity)
            outline = white(color, opacity * .48)
            draw.rounded_rectangle((x, cy + 10, x + width, cy + 88), radius=22,
                                   fill=fill, outline=outline, width=3)
            text(draw, (x + width / 2, cy + 49), label, F["small"], bright, "mm")
            x += width + 32
        text(draw, (cx, cy + 170), "ONE ADAPTER · UPSTREAM UNTOUCHED", F["small"], soft, "mm")
    elif index == 1:
        phases = ("OBSERVE", "PROPOSE", "ACT", "REFLECT")
        for j, phase in enumerate(phases):
            angle = -math.pi / 2 + j * math.pi / 2
            px, py = cx + math.cos(angle) * 190, cy + 100 + math.sin(angle) * 115
            text(draw, (px, py), phase, F["small"], bright, "mm")
        arrow_box = (cx - 75, cy + 25, cx + 75, cy + 175)
        draw.arc(arrow_box, 35, 330, fill=bright, width=7)
        arrow_angle = math.radians(330)
        arrow_x = cx + 75 * math.cos(arrow_angle)
        arrow_y = cy + 100 + 75 * math.sin(arrow_angle)
        draw.polygon(((arrow_x, arrow_y), (arrow_x - 22, arrow_y - 3),
                      (arrow_x - 8, arrow_y + 18)), fill=bright)
        text(draw, (cx, cy + 250), "THE HARNESS EDITS ITSELF", F["small"], soft, "mm")
    elif index == 2:
        sample = EPISODES[8:20]
        values = [len(item["steps"]) for item in sample]
        low, high = min(values), max(values)
        visible = max(2, min(len(values), int(ui.clamp(progress) * len(values)) + 1))
        points = []
        for j, value in enumerate(values[:visible]):
            px = cx - 245 + j * 490 / (len(values) - 1)
            py = cy + 165 - (value - low) / max(1, high - low) * 210
            points.append((px, py))
        draw.line(points, fill=bright, width=7, joint="curve")
        for j, (px, py) in enumerate(points):
            rr = 13 if j == len(points) - 1 else 8
            draw.ellipse((px - rr, py - rr, px + rr, py + rr), fill=bright)
        text(draw, (cx, cy + 245), "TRACE · DIFF · SCORE · EPISODE", F["small"], soft, "mm")
    else:
        scores = (("EP 04", .33), ("EP 08", .67), ("EP 12", 1.0))
        for j, (episode, score) in enumerate(scores):
            x = cx - 245 + j * 185
            height = 190 * score * ui.ease((progress - j * .08) / .45)
            draw.rounded_rectangle((x, cy + 175 - height, x + 120, cy + 175), radius=16,
                                   fill=white(color, opacity * (.48 + score * .42)))
            text(draw, (x + 60, cy + 215), episode, F["micro"], soft, "mm")
            text(draw, (x + 60, cy + 135 - height), f"{score:.2f}", F["small"], bright, "mm")
        text(draw, (cx, cy + 275), "MEASURE · SNAPSHOT · COMPARE", F["small"], soft, "mm")


def feature_orb(draw: ImageDraw.ImageDraw, index: int, position: tuple[float, float],
                radius: float, content_opacity: float, progress: float) -> None:
    node(draw, position, radius, FEATURES[index]["color"], progress)
    if radius > 310 and content_opacity > .01:
        feature_content(draw, index, position, content_opacity, progress)


def route(draw: ImageDraw.ImageDraw, count: int) -> None:
    for index in range(max(0, count - 1)):
        edge(draw, FINAL_POINTS[FEATURE_NODES[index]], FINAL_POINTS[FEATURE_NODES[index + 1]], 1)
    for index in range(count):
        node(draw, FINAL_POINTS[FEATURE_NODES[index]], 22, FEATURES[index]["color"])


def feature_frame(t: float) -> Image.Image:
    index = min(3, int(t // 6))
    local = t - index * 6
    label_index = index + 1 if index < 3 and local >= 5.5 else index
    image = paper(t, FEATURES[label_index]["cap"])
    draw = ImageDraw.Draw(image)
    route(draw, index)

    target = FINAL_POINTS[FEATURE_NODES[index]]
    if index == 0 and local < .9:
        grow = ui.smooth(local / .9)
        position = point_lerp(target, CENTER, grow)
        radius = lerp(22, 365, grow)
        content_alpha = ui.smooth((local - .48) / .36)
    elif local < 4.35:
        position, radius = CENTER, 365
        content_alpha = 1
    else:
        shrink = ui.smooth((local - 4.35) / .8)
        position = point_lerp(CENTER, target, shrink)
        radius = lerp(365, 22, shrink)
        content_alpha = 1 - ui.smooth((local - 4.08) / .42)
    feature_orb(draw, index, position, radius, content_alpha, local / 6)

    if local >= 5.0:
        route(draw, index + 1)
    if index < 3 and local >= 4.9:
        next_target = FINAL_POINTS[FEATURE_NODES[index + 1]]
        edge(draw, target, next_target, (local - 4.9) / .62, FEATURES[index + 1]["color"])
        grow = ui.smooth((local - 5.35) / .65)
        if grow > 0:
            next_position = point_lerp(next_target, CENTER, grow)
            feature_orb(draw, index + 1, next_position, lerp(22, 365, grow),
                        ui.smooth((grow - .88) / .12), grow)
    return image


def official_color(index: int) -> str:
    return ui.GREEN if index in (1, 4, 6) else ui.INK


def logo_frame(t: float) -> Image.Image:
    local = t - 24
    image = paper(t, "OPEN SOURCE  /  MIT")
    draw = ImageDraw.Draw(image)
    reveal = ui.smooth(local / 1.0)
    for a, b in LOGO_EDGES:
        color = ui.mix_hex(ui.BG, ui.SOFT, reveal)
        draw.line((*FINAL_POINTS[a], *FINAL_POINTS[b]), fill=color, width=6)
    for index, position in enumerate(FINAL_POINTS):
        if index in FEATURE_NODES:
            feature_index = FEATURE_NODES.index(index)
            color = ui.mix_hex(FEATURES[feature_index]["color"], official_color(index), reveal)
        else:
            color = ui.mix_hex(ui.BG, official_color(index), reveal)
        node(draw, position, 22, color, local / 6)

    copy = ui.smooth((local - .55) / .8)
    ink = ui.mix_hex(ui.BG, ui.INK, copy)
    green = ui.mix_hex(ui.BG, ui.GREEN, copy)
    ui.text(draw, (780, 310), "PROTEUS", F["body"], ink, "lm", tracking=12)
    text(draw, (780, 470), "v0.1.0", F["hero"], green, "lm")
    text(draw, (780, 585), "is released.", F["title"], ink, "lm")
    text(draw, (785, 720), "PLUG IN ANY HARNESS.", F["body"], green, "lm")
    text(draw, (785, 790), "EVOLVE · VISUALIZE · ANALYZE", F["small"], ink, "lm")
    text(draw, (785, 875), "PROTEUS-EVOLVE.GITHUB.IO", F["small"], green, "lm")
    return image


def render_frame(frame: int) -> Image.Image:
    t = frame / FPS
    return (feature_frame(t) if t < 24 else logo_frame(t)).convert("RGB")


def midi(note: int) -> float:
    return 440 * 2 ** ((note - 69) / 12)


def original_jazz_piano(path: Path) -> None:
    """Synthesize an original 12-bar light-jazz piano cue (no sampled material)."""
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
    for bar_index, (chord, bass) in enumerate(progression):
        start = bar_index * bar
        add_note(start, beat * 1.7, bass, .82, -.28)
        add_note(start + beat * 2, beat * 1.4, bass + 7, .64, -.22)
        for offset, strength in ((0, .68), (beat * 1.5, .48), (beat * 2.75, .56)):
            for j, note in enumerate(chord):
                add_note(start + offset + j * .008, beat * 1.35, note, strength, -.08 + j * .04)
        if bar_index % 2 == 0:
            phrase = melody[(bar_index * 2) % len(melody):]
            for step in range(4):
                swing = .08 if step % 2 else 0
                add_note(start + beat * (step + .35) + swing, beat * .72,
                         phrase[step % len(phrase)], .52, .30)

    dry = track.copy()
    for delay, gain in ((.075, .17), (.145, .11), (.235, .07)):
        samples = int(delay * SAMPLE_RATE)
        track[samples:] += dry[:-samples] * gain
    fade = np.ones(total)
    fade[:SAMPLE_RATE] = np.linspace(0, 1, SAMPLE_RATE)
    fade[-SAMPLE_RATE:] = np.linspace(1, 0, SAMPLE_RATE)
    track *= fade[:, None]
    peak = np.max(np.abs(track)) or 1
    pcm = (np.tanh(track / peak * 1.25) * .78 * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


def main() -> None:
    temporary = Path(tempfile.mkdtemp(prefix="proteus-release-orbs-"))
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
