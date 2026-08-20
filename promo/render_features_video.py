#!/usr/bin/env python3
"""Render the 15-second Proteus product feature film.

The video is deliberately generated from vectors/text so it stays crisp, matches
the website, and remains easy to revise without a video-editing project file.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


W, H = 1920, 1080
FPS, DURATION = 30, 15
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "proteus-features-15s.mp4"

BG = "#F2EEE3"
PANEL = "#FAF7EF"
INK = "#211E17"
SOFT = "#7A7263"
DIM = "#A8A091"
RULE = "#DCD5C4"
GREEN = "#2E5D43"
GREEN_HI = "#3E7A58"
BAD = "#A8402E"
BLUE = "#4F6DF5"
ORANGE = "#E8833A"
LEAF = "#3FA96E"
VIOLET = "#9A7BD6"

SANS_PATH = "/System/Library/Fonts/SFNS.ttf"
MONO_PATH = "/System/Library/Fonts/SFNSMono.ttf"
SERIF_PATH = "/System/Library/Fonts/NewYork.ttf"


def font(path: str, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size, index=index)


F = {
    "micro": font(MONO_PATH, 19),
    "small": font(MONO_PATH, 24),
    "mono": font(MONO_PATH, 28),
    "mono_b": font(MONO_PATH, 31),
    "body": font(SANS_PATH, 34),
    "body_b": font(SANS_PATH, 36),
    "sub": font(SERIF_PATH, 49),
    "title": font(SERIF_PATH, 84),
    "hero": font(SERIF_PATH, 111),
    "wordmark": font(MONO_PATH, 43),
}


def clamp(v: float, lo: float = 0, hi: float = 1) -> float:
    return max(lo, min(hi, v))


def ease(v: float) -> float:
    v = clamp(v)
    return 1 - (1 - v) ** 3


def smooth(v: float) -> float:
    v = clamp(v)
    return v * v * (3 - 2 * v)


def scene_alpha(t: float, start: float, end: float, fade: float = 0.24) -> float:
    return min(smooth((t - start) / fade), smooth((end - t) / fade))


def mix_hex(a: str, b: str, v: float) -> str:
    v = clamp(v)
    aa = tuple(int(a[i : i + 2], 16) for i in (1, 3, 5))
    bb = tuple(int(b[i : i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * v):02x}" for x, y in zip(aa, bb))


def layer() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    return im, ImageDraw.Draw(im)


def composite(base: Image.Image, overlay: Image.Image, alpha: float = 1) -> None:
    if alpha < 1:
        overlay = overlay.copy()
        overlay.putalpha(overlay.getchannel("A").point(lambda x: int(x * alpha)))
    base.alpha_composite(overlay)


def text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], value: str,
         face: ImageFont.FreeTypeFont, fill: str = INK, anchor: str = "la",
         tracking: int = 0) -> None:
    if tracking == 0:
        draw.text(xy, value, font=face, fill=fill, anchor=anchor)
        return
    x, y = xy
    widths = [draw.textlength(ch, font=face) for ch in value]
    total = sum(widths) + tracking * max(0, len(value) - 1)
    if anchor[0] == "m":
        x -= total / 2
    elif anchor[0] == "r":
        x -= total
    for ch, width in zip(value, widths):
        draw.text((x, y), ch, font=face, fill=fill, anchor="la")
        x += width + tracking


def rounded(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float],
            radius: int = 18, fill: str = PANEL, outline: str = RULE, width: int = 2) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def pill(draw: ImageDraw.ImageDraw, xy: tuple[float, float], label: str, color: str,
         active: bool = False, pad_x: int = 20, pad_y: int = 11) -> float:
    x, y = xy
    tw = draw.textlength(label, font=F["small"])
    box = (x, y, x + tw + 2 * pad_x, y + 31 + 2 * pad_y)
    rounded(draw, box, 11, mix_hex(BG, color, 0.13) if active else BG,
            color if active else RULE, 2)
    text(draw, (x + pad_x, y + pad_y + 16), label, F["small"], color if active else SOFT, "lm")
    return box[2]


def logo_mark(draw: ImageDraw.ImageDraw, x: float, y: float, scale: float = 1) -> None:
    pts = [(0, 0), (84, 1), (147, 36), (0, 72), (156, 102), (43, 111),
           (96, 139), (0, 149), (0, 224)]
    edges = [(0, 1), (0, 3), (1, 2), (1, 5), (2, 4), (2, 5), (3, 5),
             (3, 7), (4, 6), (5, 6), (5, 7), (6, 7), (7, 8)]
    p = [(x + px * scale, y + py * scale) for px, py in pts]
    for a, b in edges:
        draw.line((p[a], p[b]), fill=SOFT, width=max(2, round(3 * scale)))
    for i, (px, py) in enumerate(p):
        r = 9 * scale
        color = GREEN if i in (1, 4, 6) else INK
        draw.ellipse((px - r, py - r, px + r, py + r), fill=color)


def global_chrome(draw: ImageDraw.ImageDraw, t: float) -> None:
    # A restrained drafting grid gives every frame visual continuity.
    for x in range(120, W, 120):
        draw.line((x, 0, x, H), fill="#EDE7D9", width=1)
    for y in range(120, H, 120):
        draw.line((0, y, W, y), fill="#EDE7D9", width=1)
    text(draw, (86, 61), "PROTEUS", F["micro"], INK, "lm", tracking=5)
    text(draw, (W - 86, 61), "HARNESS EVOLUTION / 00:15", F["micro"], SOFT, "rm")
    draw.line((86, 92, W - 86, 92), fill=RULE, width=2)
    draw.line((86, H - 72, W - 86, H - 72), fill=RULE, width=2)
    draw.rectangle((86, H - 73, 86 + (W - 172) * clamp(t / DURATION), H - 70), fill=GREEN)


def draw_intro(t: float) -> Image.Image:
    im, d = layer()
    local = t
    rise = 38 * (1 - ease(local / 0.7))
    logo_mark(d, 887, 174 + rise, 0.78)
    a1 = smooth(local / 0.52)
    a2 = smooth((local - 0.36) / 0.58)
    text(d, (W / 2, 470 + rise), "Your agent evolves.", F["hero"], mix_hex(BG, INK, a1), "mm")
    text(d, (W / 2, 600 + rise), "Why doesn’t its harness?", F["hero"], mix_hex(BG, GREEN_HI, a2), "mm")
    text(d, (W / 2, 750), "THE OPEN-SOURCE SELF-EVOLUTION FRAMEWORK FOR AGENT HARNESSES",
         F["micro"], mix_hex(BG, SOFT, smooth((local - 0.7) / 0.5)), "mm", tracking=2)
    return im


def selector_card(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int], cap: str,
                  items: list[str], active: int, accent: str, progress: float) -> None:
    rounded(d, box, 24, PANEL, RULE, 2)
    x1, y1, x2, y2 = box
    text(d, (x1 + 35, y1 + 40), cap, F["micro"], SOFT, "lm", tracking=3)
    d.line((x1 + 34, y1 + 74, x2 - 34, y1 + 74), fill=RULE, width=2)
    for i, name in enumerate(items):
        y = y1 + 111 + i * 92
        visible = smooth((progress - i * 0.12) / 0.32)
        c = mix_hex(PANEL, accent if i == active else SOFT, visible)
        if i == active:
            rounded(d, (x1 + 28, y - 28, x2 - 28, y + 39), 13,
                    mix_hex(PANEL, accent, 0.11 * visible), mix_hex(RULE, accent, visible), 2)
        d.ellipse((x1 + 46, y - 4, x1 + 56, y + 6), fill=c)
        text(d, (x1 + 78, y), name, F["mono"], c, "lm")


def draw_choose(t: float) -> Image.Image:
    im, d = layer()
    p = ease((t - 1.36) / 0.62)
    text(d, (W / 2, 174), "Choose your harness × model.", F["title"], INK, "mm")
    text(d, (W / 2, 248), "Start from an open-source harness—or bring your own.", F["body"], SOFT, "mm")
    dx = 70 * (1 - p)
    selector_card(d, (275 - round(dx), 330, 824 - round(dx), 750), "01 / HARNESS",
                  ["DSH", "PI", "CUSTOM HARNESS"], 0, GREEN, p)
    selector_card(d, (1096 + round(dx), 330, 1645 + round(dx), 750), "02 / MODEL",
                  ["DEEPSEEK", "CLAUDE", "YOUR MODEL"], 0, BLUE, p)
    d.line((886, 540, 1034, 540), fill=RULE, width=2)
    d.ellipse((928, 498, 992, 562), fill=BG, outline=GREEN, width=2)
    text(d, (960, 530), "×", F["sub"], GREEN_HI, "mm")
    text(d, (W / 2, 846), "HARNESS + MODEL BECOME THE EVOLVING SYSTEM", F["micro"], GREEN, "mm", tracking=3)
    return im


def draw_goals(t: float) -> Image.Image:
    im, d = layer()
    local = t - 3.2
    text(d, (W / 2, 178), "Set goals in plain language.", F["title"], INK, "mm")
    text(d, (W / 2, 252), "One objective—or several at once.", F["body"], SOFT, "mm")
    cards = [
        ("01", "Make the harness more reliable.", GREEN),
        ("02", "Improve SWE-bench performance.", BLUE),
    ]
    for i, (num, value, accent) in enumerate(cards):
        y = 354 + i * 180
        show = ease((local - i * 0.32) / 0.45)
        x = 260 + 50 * (1 - show)
        rounded(d, (x, y, 1660, y + 132), 18, PANEL, mix_hex(RULE, accent, show), 2)
        text(d, (x + 31, y + 33), f"GOAL {num}", F["micro"], accent, "lm", tracking=2)
        max_chars = round(len(value) * clamp((local - 0.18 - i * 0.32) / 0.7))
        typed = value[:max_chars]
        text(d, (x + 232, y + 66), typed, F["body_b"], INK, "lm")
        if max_chars < len(value) and max_chars > 0:
            cursor_x = x + 232 + d.textlength(typed, font=F["body_b"])
            d.rectangle((cursor_x + 5, y + 43, cursor_x + 8, y + 89), fill=accent)
    plus_a = smooth((local - 0.92) / 0.35)
    rounded(d, (690, 744, 1230, 814), 35, BG, mix_hex(BG, RULE, plus_a), 2)
    text(d, (960, 779), "+  ADD ANOTHER GOAL", F["small"], mix_hex(BG, SOFT, plus_a), "mm", tracking=2)
    return im


def draw_evolution(t: float) -> Image.Image:
    im, d = layer()
    local = t - 5.05
    text(d, (W / 2, 158), "Proteus evolves the harness itself.", F["title"], INK, "mm")
    text(d, (W / 2, 226), "Every episode reads, edits, tests, and reflects.", F["body"], SOFT, "mm")

    # File surface.
    rounded(d, (105, 292, 575, 822), 20, PANEL, RULE, 2)
    text(d, (140, 330), "EVOLVING SURFACE", F["micro"], SOFT, "lm", tracking=2)
    d.line((140, 365, 540, 365), fill=RULE, width=2)
    files = [
        ("notes/", "memory.md", BLUE),
        ("skills/", "verify.md", LEAF),
        ("tools/", "health_check.py", ORANGE),
        ("instructions/", "agent.md", VIOLET),
    ]
    for i, (folder, name, color) in enumerate(files):
        y = 418 + i * 89
        show = smooth((local - i * 0.32) / 0.45)
        text(d, (143, y), folder + name, F["small"], mix_hex(PANEL, color, show), "lm")
        if show > 0.08:
            text(d, (525, y), f"+{2 + i * 3}", F["small"], color, "rm")

    # Episode rail.
    rounded(d, (616, 292, 1265, 822), 20, PANEL, RULE, 2)
    text(d, (650, 330), "EVOLUTION RUN", F["micro"], SOFT, "lm", tracking=2)
    text(d, (1230, 330), "LIVE", F["micro"], GREEN, "rm", tracking=2)
    d.line((650, 365, 1230, 365), fill=RULE, width=2)
    rail_y = 454
    d.line((676, rail_y, 1205, rail_y), fill=RULE, width=5)
    active = min(12, max(1, int(local * 3.2) + 1))
    colors = [BLUE, LEAF, ORANGE, VIOLET]
    for i in range(12):
        x = 682 + i * 47
        on = i < active
        c = colors[i % 4] if on else DIM
        r = 11 if i == active - 1 else 7
        d.ellipse((x - r, rail_y - r, x + r, rail_y + r), fill=c)
    text(d, (650, 503), f"EPISODE {active:02d} / 30", F["mono_b"], INK, "lm")
    status = [
        ("inspect", "harness state", SOFT),
        ("edit", files[(active - 1) % 4][1], colors[(active - 1) % 4]),
        ("test", f"{12 + active} checks passed", GREEN),
        ("reflect", "retain useful change", GREEN_HI),
    ]
    for i, (verb, val, color) in enumerate(status):
        y = 570 + i * 54
        recent = clamp(local * 4 - i - 0.2)
        text(d, (653, y), f"{verb:>8}", F["small"], color if recent else DIM, "lm")
        text(d, (820, y), val, F["small"], INK if recent else DIM, "lm")

    # Diff panel.
    rounded(d, (1306, 292, 1815, 822), 20, PANEL, RULE, 2)
    text(d, (1340, 330), "CURRENT CHANGE", F["micro"], SOFT, "lm", tracking=2)
    d.line((1340, 365, 1780, 365), fill=RULE, width=2)
    diff_lines = [
        ("+ check dependency health", GREEN),
        ("+ verify modified files", GREEN),
        ("+ write audit summary", GREEN),
        ("- assume success", BAD),
        ("+ prove the change works", GREEN_HI),
    ]
    for i, (line, color) in enumerate(diff_lines):
        y = 421 + i * 65
        show = smooth((local - 0.35 - i * 0.22) / 0.38)
        text(d, (1342, y), line, F["small"], mix_hex(PANEL, color, show), "lm")
    text(d, (1340, 765), "snapshot / ep_%02d" % active, F["small"], BLUE, "lm")
    return im


def draw_measure(t: float) -> Image.Image:
    im, d = layer()
    local = t - 9.45
    text(d, (W / 2, 165), "Every episode measured.", F["title"], INK, "mm")
    text(d, (W / 2, 255), "Every snapshot saved.", F["title"], GREEN_HI, "mm")

    rounded(d, (145, 342, 1085, 820), 22, PANEL, RULE, 2)
    text(d, (184, 385), "EXAMPLE EVALUATION / GOAL SCORE", F["micro"], SOFT, "lm", tracking=2)
    text(d, (1038, 385), "BEST 79", F["mono_b"], GREEN, "rm")
    chart = (190, 464, 1040, 742)
    for i in range(4):
        y = chart[1] + i * (chart[3] - chart[1]) / 3
        d.line((chart[0], y, chart[2], y), fill=RULE, width=1)
    values = [52, 54, 53, 58, 60, 63, 62, 67, 71, 70, 75, 79]
    n = max(2, min(len(values), round(2 + local * 4.7)))
    pts = []
    for i, value in enumerate(values[:n]):
        x = chart[0] + i * (chart[2] - chart[0]) / (len(values) - 1)
        y = chart[3] - (value - 45) / 40 * (chart[3] - chart[1])
        pts.append((x, y))
    if len(pts) > 1:
        d.line(pts, fill=GREEN, width=6, joint="curve")
    for i, (x, y) in enumerate(pts):
        color = [BLUE, LEAF, ORANGE, VIOLET][i % 4]
        d.ellipse((x - 8, y - 8, x + 8, y + 8), fill=color)
    text(d, (190, 780), "EP 01", F["micro"], DIM, "lm")
    text(d, (1040, 780), "EP 30", F["micro"], DIM, "rm")

    text(d, (1172, 371), "SNAPSHOTS", F["micro"], SOFT, "lm", tracking=2)
    snaps = [("EP 06", "baseline", "52"), ("EP 18", "candidate", "67"), ("EP 30", "best", "79")]
    for i, (ep, label, score) in enumerate(snaps):
        y = 424 + i * 128
        show = ease((local - 0.18 - i * 0.24) / 0.42)
        x = 1170 + 36 * (1 - show)
        active = i == 2
        rounded(d, (x, y, 1775, y + 100), 16,
                mix_hex(PANEL, GREEN, 0.10 if active else 0), GREEN if active else RULE, 2)
        text(d, (x + 27, y + 50), ep, F["small"], GREEN if active else SOFT, "lm")
        text(d, (x + 178, y + 50), label, F["small"], INK, "lm")
        text(d, (1740, y + 50), score, F["mono_b"], GREEN_HI if active else SOFT, "rm")
    text(d, (1472, 835), "COMPARE  ·  INSPECT  ·  REUSE", F["micro"], SOFT, "mm", tracking=2)
    return im


def draw_outro(t: float) -> Image.Image:
    im, d = layer()
    local = t - 12.2
    p = ease(local / 0.72)
    logo_mark(d, 372, 250 + 30 * (1 - p), 1.66)
    d.line((700, 245, 700, 798), fill=mix_hex(BG, RULE, p), width=2)
    text(d, (790, 350), "PROTEUS", F["wordmark"], mix_hex(BG, INK, p), "lm", tracking=10)
    text(d, (790, 487), "Choose your harness.", F["title"], mix_hex(BG, INK, p), "lm")
    text(d, (790, 591), "Start evolving.", F["title"], mix_hex(BG, GREEN_HI, p), "lm")
    text(d, (793, 700), "OPEN SOURCE  /  PROTEUS-EVOLVE.GITHUB.IO", F["micro"],
         mix_hex(BG, SOFT, smooth((local - 0.42) / 0.55)), "lm", tracking=2)
    text(d, (793, 753), "GITHUB.COM/PROTEUS-EVOLVE/PROTEUS", F["micro"],
         mix_hex(BG, GREEN, smooth((local - 0.58) / 0.55)), "lm", tracking=2)
    return im


def render_frame(frame: int) -> Image.Image:
    t = frame / FPS
    base = Image.new("RGBA", (W, H), BG)
    chrome, d = layer()
    global_chrome(d, t)
    composite(base, chrome)

    scenes = [
        (0.0, 1.75, draw_intro),
        (1.48, 3.55, draw_choose),
        (3.27, 5.35, draw_goals),
        (5.08, 9.78, draw_evolution),
        (9.5, 12.55, draw_measure),
        (12.25, 15.01, draw_outro),
    ]
    for start, end, fn in scenes:
        if start <= t <= end:
            composite(base, fn(t), scene_alpha(t, start, end))
    return base.convert("RGB")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="proteus-features-"))
    try:
        for frame in range(FPS * DURATION):
            render_frame(frame).save(tmp / f"frame-{frame:04d}.png", compress_level=2)
            if frame % 90 == 0:
                print(f"rendered {frame:03d}/{FPS * DURATION}", flush=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "warning", "-framerate", str(FPS),
                "-i", str(tmp / "frame-%04d.png"), "-c:v", "libx264", "-preset", "slow",
                "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                "-t", str(DURATION), str(OUT),
            ],
            check=True,
        )
    finally:
        shutil.rmtree(tmp)
    print(OUT)


if __name__ == "__main__":
    main()
