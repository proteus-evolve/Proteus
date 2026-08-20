#!/usr/bin/env python3
"""Render the v0.1.0 launch cut with the DSH audio-evolution teaser."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

import render_features_video as film


OUT = Path(__file__).resolve().parent / "proteus-v0.1.0-dsh-teaser-15s.mp4"


def launch_badge(t: float) -> Image.Image:
    image, draw = film.layer()
    alpha = film.smooth((t - 0.12) / 0.45) * film.smooth((1.58 - t) / 0.25)
    color = film.mix_hex(film.BG, film.GREEN, alpha)
    film.rounded(draw, (760, 286, 1160, 350), 32, film.BG, color, 2)
    film.text(draw, (960, 318), "V0.1.0  /  NOW LIVE", film.F["small"], color, "mm", tracking=2)
    return image


def teaser_frame(t: float) -> Image.Image:
    canvas = Image.new("RGBA", (film.W, film.H), film.BG)
    chrome, chrome_draw = film.layer()
    film.global_chrome(chrome_draw, t)
    film.composite(canvas, chrome)

    layer, draw = film.layer()
    local = t - 12.15
    progress = film.ease(local / 0.7)
    film.logo_mark(draw, 215, 282 + 28 * (1 - progress), 1.42)
    draw.line((565, 235, 565, 820), fill=film.mix_hex(film.BG, film.RULE, progress), width=2)
    film.text(draw, (665, 254), "NEXT PUBLIC EVOLUTION", film.F["micro"],
              film.mix_hex(film.BG, film.GREEN, progress), "lm", tracking=3)
    film.text(draw, (665, 365), "DeepSeek Harness", film.F["title"],
              film.mix_hex(film.BG, film.INK, progress), "lm")
    film.text(draw, (665, 466), "just learned to see.", film.F["title"],
              film.mix_hex(film.BG, film.INK, progress), "lm")
    film.text(draw, (665, 603), "Can it evolve itself to hear?", film.F["sub"],
              film.mix_hex(film.BG, film.GREEN_HI, film.smooth((local - 0.28) / 0.55)), "lm")
    film.text(draw, (668, 708), "FULL TRACE · EPISODE BY EPISODE · LIVE ON THE PROTEUS SITE",
              film.F["micro"], film.mix_hex(film.BG, film.SOFT,
                                             film.smooth((local - 0.58) / 0.55)),
              "lm", tracking=2)
    film.text(draw, (668, 764), "PIP INSTALL PROTEUS-EVOLVE  /  PROTEUS-EVOLVE.GITHUB.IO",
              film.F["micro"], film.mix_hex(film.BG, film.GREEN,
                                             film.smooth((local - 0.78) / 0.55)),
              "lm", tracking=1)
    film.composite(canvas, layer)
    return canvas.convert("RGB")


def render_frame(frame: int) -> Image.Image:
    t = frame / film.FPS
    if t >= 12.15:
        return teaser_frame(t)
    image = film.render_frame(frame).convert("RGBA")
    if t <= 1.7:
        film.composite(image, launch_badge(t))
    return image.convert("RGB")


def main() -> None:
    temporary = Path(tempfile.mkdtemp(prefix="proteus-release-teaser-"))
    try:
        for frame in range(film.FPS * film.DURATION):
            render_frame(frame).save(temporary / f"frame-{frame:04d}.png", compress_level=2)
            if frame % 90 == 0:
                print(f"rendered {frame:03d}/{film.FPS * film.DURATION}", flush=True)
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "warning", "-framerate", str(film.FPS),
            "-i", str(temporary / "frame-%04d.png"), "-c:v", "libx264", "-preset", "slow",
            "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-t", str(film.DURATION), str(OUT),
        ], check=True)
    finally:
        shutil.rmtree(temporary)
    print(OUT)


if __name__ == "__main__":
    main()
