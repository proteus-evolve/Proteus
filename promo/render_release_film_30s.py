#!/usr/bin/env python3
"""Render the silent continuous-orb Proteus v0.1.0 release film.

Each feature is one expanded evolution node. The camera pulls back, follows the growing
edge and its live node through world space, then pushes into the next feature. At the end
the camera stays wide and the accumulated trace resolves into the Proteus mark.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import render_features_video as ui


W, H = 1920, 1080
FPS, DURATION = 30, 30
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
    "detail": ImageFont.truetype(SERIF_BOLD, 30),
    "feed": ImageFont.truetype(MONO_BOLD, 23),
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
ROUTE_EDGES = ((0, 1), (1, 2), (2, 4))
LOGO_ORIGIN, LOGO_SCALE = (205, 235), 2.45
FINAL_POINTS = tuple((LOGO_ORIGIN[0] + x * LOGO_SCALE,
                      LOGO_ORIGIN[1] + y * LOGO_SCALE) for x, y in LOGO_POINTS)
CENTER = (960, 555)
WORLD_NODE_RADIUS, CLOSE_SCALE, FOLLOW_SCALE = 22, 16.6, 5.5
FINAL_CAMERA = ((CENTER[0] - LOGO_ORIGIN[0]) / LOGO_SCALE,
                (CENTER[1] - LOGO_ORIGIN[1]) / LOGO_SCALE)
DOME_CENTER, DOME_RADIUS = (20, 540), 1050


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


def project(world: tuple[float, float], camera: tuple[float, float], scale: float) -> tuple[float, float]:
    return CENTER[0] + (world[0] - camera[0]) * scale, \
        CENTER[1] + (world[1] - camera[1]) * scale


def world_edge(draw: ImageDraw.ImageDraw, start: tuple[float, float], end: tuple[float, float],
               camera: tuple[float, float], scale: float, progress: float = 1,
               color: str = ui.SOFT) -> tuple[float, float]:
    tip = point_lerp(start, end, ui.smooth(progress))
    a, b = project(start, camera, scale), project(tip, camera, scale)
    draw.line((*a, *b), fill=color, width=max(3, min(8, round(scale * .46))))
    return tip


def world_node(draw: ImageDraw.ImageDraw, world: tuple[float, float], camera: tuple[float, float],
               scale: float, color: str, pulse: float = 0) -> tuple[float, float]:
    position = project(world, camera, scale)
    node(draw, position, WORLD_NODE_RADIUS * scale, color, pulse)
    return position


def draw_follow_world(draw: ImageDraw.ImageDraw, index: int, camera: tuple[float, float],
                      scale: float, line_progress: float, pulse: float) -> tuple[float, float] | None:
    for route_index in range(index):
        a, b = ROUTE_EDGES[route_index]
        world_edge(draw, LOGO_POINTS[a], LOGO_POINTS[b], camera, scale)
    for feature_index in range(index + 1):
        logo_index = FEATURE_NODES[feature_index]
        world_node(draw, LOGO_POINTS[logo_index], camera, scale,
                   FEATURES[feature_index]["color"], pulse)
    if index >= 3 or line_progress <= 0:
        return None
    a, b = ROUTE_EDGES[index]
    tip = world_edge(draw, LOGO_POINTS[a], LOGO_POINTS[b], camera, scale,
                     line_progress, FEATURES[index + 1]["color"])
    world_node(draw, tip, camera, scale, FEATURES[index + 1]["color"], pulse)
    return tip


def draw_logo_world(draw: ImageDraw.ImageDraw, camera: tuple[float, float], scale: float,
                    reveal: float, pulse: float) -> None:
    reveal = ui.smooth(reveal)
    for a, b in LOGO_EDGES:
        amount = 1 if (a, b) in ROUTE_EDGES else reveal
        color = ui.mix_hex(ui.BG, ui.SOFT, amount)
        world_edge(draw, LOGO_POINTS[a], LOGO_POINTS[b], camera, scale, color=color)
    for logo_index, world in enumerate(LOGO_POINTS):
        if logo_index in FEATURE_NODES:
            feature_index = FEATURE_NODES.index(logo_index)
            color = ui.mix_hex(FEATURES[feature_index]["color"],
                               official_color(logo_index), reveal)
        else:
            color = ui.mix_hex(ui.BG, official_color(logo_index), reveal)
        world_node(draw, world, camera, scale, color, pulse)


def detail_color(color: str, opacity: float, *, on_disc: bool = False) -> str:
    base = color if on_disc else ui.BG
    target = "#FFFFFF" if on_disc else ui.INK
    return ui.mix_hex(base, target, ui.clamp(opacity))


def detail_kv(draw: ImageDraw.ImageDraw, x: float, y: float, label: str, value: str,
              opacity: float, accent: str | None = None) -> float:
    cap = ui.mix_hex(ui.BG, ui.SOFT, opacity)
    body = ui.mix_hex(ui.BG, accent or ui.INK, opacity)
    text(draw, (x, y), label, F["micro"], cap, "la")
    lines = value.split("\n")
    for offset, line in enumerate(lines):
        text(draw, (x, y + 42 + offset * 38), line, F["detail"], body, "la")
    return y + 42 + len(lines) * 38 + 32


def side_details(draw: ImageDraw.ImageDraw, index: int, opacity: float) -> None:
    if opacity <= .01:
        return
    nav = ui.mix_hex(ui.BG, ui.INK, opacity)
    text(draw, (W - 76, 145), "‹ PREV   NEXT ›   × CLOSE", F["micro"], nav, "ra")
    left_x, right_x, top = 1110, 1515, 250
    left_sets = (
        (("HARNESS", "DSH · Pi · custom"), ("MODEL", "choose your model"),
         ("ADAPTER", "one small adapter"), ("UPSTREAM", "untouched")),
        (("EPISODE", "context-fresh"), ("PHASES", "observe · propose\nact · reflect"),
         ("BOUNDARY", "only files survive"), ("SNAPSHOT", "one git commit")),
        (("LIVE VIEW", "camera follows\nthe active episode"), ("TRACE", "phase · tool · target"),
         ("DIFF", "added · revised\ndropped"), ("DETAIL", "expand any point")),
        (("EVALUATORS", "hidden or observed"), ("TARGETS", "one benchmark\nor several goals"),
         ("HISTORY", "every snapshot kept"), ("EXPORT", "compare · inspect\nreuse")),
    )
    right_sets = (
        (("SANDBOX", "prepared · pinned"), ("GOAL", "natural language"),
         ("CUSTOM", "bring any harness"), ("RESULT", "same evolution loop")),
        (("EVOLVING SURFACE", "code · instructions"), ("TOOLS", "read · edit · test"),
         ("FEEDBACK", "last episode score"), ("STATE", "versioned harness")),
        (("EPISODE POINT", "completed snapshot"), ("MEASUREMENT", "score · tool calls"),
         ("FILES TOUCHED", "public structural diff"), ("PLAYBACK", "pause · scrub\nreplay")),
        (("MEASURE", "capability · behavior"), ("SNAPSHOTS", "baseline · candidate\nbest"),
         ("COMPARE", "trajectory · versions"), ("CLAIM", "evidence attached")),
    )
    y = top
    for label, value in left_sets[index]:
        y = detail_kv(draw, left_x, y, label, value, opacity,
                      FEATURES[index]["color"] if label in {"HARNESS", "EPISODE", "LIVE VIEW", "EVALUATORS"} else None)
    y = top
    for label, value in right_sets[index]:
        y = detail_kv(draw, right_x, y, label, value, opacity,
                      FEATURES[index]["color"] if label in {"SANDBOX", "EVOLVING SURFACE", "EPISODE POINT", "MEASURE"} else None)


def dome_feed(draw: ImageDraw.ImageDraw, index: int, opacity: float, progress: float) -> None:
    color = FEATURES[index]["color"]
    bright = detail_color(color, opacity, on_disc=True)
    muted = ui.mix_hex(color, "#FFFFFF", opacity * .62)
    x = 110
    text(draw, (x, 165), FEATURES[index]["cap"], F["micro"], muted, "la")
    text(draw, (x, 270), FEATURES[index]["title"], F["title"], bright, "la")

    if index == 0:
        text(draw, (x, 345), "Plug in an open-source harness—or bring your own.",
             F["detail"], bright, "la")
        rows = (("HARNESS", "DSH"), ("HARNESS", "PI"), ("HARNESS", "CUSTOM HARNESS"),
                ("ADAPTER", "prepare · run · read trace"), ("SOURCE", "upstream untouched"))
    elif index == 1:
        text(draw, (x, 345), "A context-fresh episode. Files carry the evolution forward.",
             F["detail"], bright, "la")
        rows = (("OBSERVE", "read harness state"), ("PROPOSE", "choose the next change"),
                ("ACT", "edit · test · verify"), ("REFLECT", "retain useful structure"),
                ("BOUNDARY", "snapshot / episode"))
    elif index == 2:
        text(draw, (x, 345), "Watch the active node, then open any completed episode.",
             F["detail"], bright, "la")
        rows = (("LIVE", "phase · tool · target"), ("EPISODE", "normalized action stream"),
                ("DIFF", "+ added · ~ revised · − dropped"), ("SCORE", "measurement beside trace"),
                ("REPLAY", "pause · scrub · inspect"))
    else:
        text(draw, (x, 345), "Measurements and snapshots turn a trajectory into evidence.",
             F["detail"], bright, "la")
        rows = (("MEASURE", "capability score"), ("COMPARE", "baseline → candidate → best"),
                ("SNAPSHOT", "one version per episode"), ("ANALYZE", "behavior · growth · reliability"),
                ("EXPORT", "reuse the evolved harness"))

    shown = max(1, min(len(rows), round(ui.ease(progress) * len(rows))))
    for row, (phase, value) in enumerate(rows[:shown]):
        y = 430 + row * 78
        text(draw, (x, y), phase, F["feed"], muted, "la")
        text(draw, (x + 220, y), value, F["small"], bright, "la")
        if row == shown - 1:
            draw.rectangle((x + 220, y + 29, x + 220 + min(500, 12 * len(value)), y + 33),
                           fill=bright)


def feature_dome(draw: ImageDraw.ImageDraw, index: int, morph: float,
                 opacity: float, progress: float) -> None:
    morph = ui.smooth(morph)
    center = point_lerp(CENTER, DOME_CENTER, morph)
    radius = lerp(WORLD_NODE_RADIUS * CLOSE_SCALE, DOME_RADIUS, morph)
    color = FEATURES[index]["color"]
    for extra, mix in ((30, .06), (15, .10)):
        draw.ellipse((center[0] - radius - extra, center[1] - radius - extra,
                      center[0] + radius + extra, center[1] + radius + extra),
                     fill=ui.mix_hex(ui.BG, color, mix))
    draw.ellipse((center[0] - radius, center[1] - radius,
                  center[0] + radius, center[1] + radius), fill=color)
    if morph > .72:
        content_opacity = opacity * ui.smooth((morph - .72) / .24)
        dome_feed(draw, index, content_opacity, progress)
        side_details(draw, index, content_opacity)


def feature_frame(t: float) -> Image.Image:
    index = min(3, int(t // 6))
    local = t - index * 6
    current = LOGO_POINTS[FEATURE_NODES[index]]
    next_world = LOGO_POINTS[FEATURE_NODES[index + 1]] if index < 3 else None

    image = paper(t, FEATURES[index]["cap"])
    draw = ImageDraw.Draw(image)
    if local < .58:
        feature_dome(draw, index, local / .58, ui.smooth(local / .44), local / 4)
        return image
    if local < 3.68:
        feature_dome(draw, index, 1, 1, local / 4)
        return image
    if local < 4.34:
        close = 1 - ui.smooth((local - 3.68) / .66)
        feature_dome(draw, index, close, close, local / 4)
        return image

    line_progress = 0.0
    if local < 4.76:
        pullback = ui.smooth((local - 4.34) / .42)
        camera, scale = current, lerp(CLOSE_SCALE, FOLLOW_SCALE, pullback)
    elif index < 3 and local < 5.45:
        line_progress = ui.smooth((local - 4.76) / .69)
        camera = point_lerp(current, next_world, line_progress)
        scale = FOLLOW_SCALE
    elif index < 3:
        line_progress = 1.0
        pushin = ui.smooth((local - 5.45) / .55)
        camera, scale = next_world, lerp(FOLLOW_SCALE, CLOSE_SCALE, pushin)
    else:
        # Last node: continue the same camera move, but pull out far enough to reveal
        # the entire connected world as the Proteus mark.
        wide = ui.smooth((local - 4.34) / 1.66)
        camera = point_lerp(current, FINAL_CAMERA, wide)
        scale = lerp(CLOSE_SCALE, LOGO_SCALE, wide)

    label_index = index + 1 if index < 3 and local >= 5.45 else index
    if label_index != index:
        image = paper(t, FEATURES[label_index]["cap"])
        draw = ImageDraw.Draw(image)
    if index == 3 and local >= 4.0:
        draw_logo_world(draw, camera, scale, (local - 4.55) / 1.3, t / 6)
    else:
        draw_follow_world(draw, index, camera, scale, line_progress, t / 6)
    return image


def official_color(index: int) -> str:
    return ui.GREEN if index in (1, 4, 6) else ui.INK


def logo_frame(t: float) -> Image.Image:
    local = t - 24
    image = paper(t, "OPEN SOURCE  /  MIT")
    draw = ImageDraw.Draw(image)
    reveal = ui.smooth(local / 1.0)
    for a, b in LOGO_EDGES:
        amount = 1 if (a, b) in ROUTE_EDGES else reveal
        color = ui.mix_hex(ui.BG, ui.SOFT, amount)
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


def main() -> None:
    temporary = Path(tempfile.mkdtemp(prefix="proteus-release-orbs-"))
    silent = temporary / "silent.mp4"
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
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "warning", "-i", str(silent),
            "-c:v", "copy", "-an",
            "-movflags", "+faststart", str(OUT),
        ], check=True)
    finally:
        shutil.rmtree(temporary)
    print(OUT)


if __name__ == "__main__":
    main()
