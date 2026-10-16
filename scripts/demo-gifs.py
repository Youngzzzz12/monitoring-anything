"""Turn the demo recordings (scripts/demo-media.mjs record) into the README GIFs in docs/screenshots.

Resamples the screencast frames to a steady rate, scales them and uses one palette per GIF (from a mosaic of frames),
so colours stay put between frames. Needs Pillow.

    python scripts/demo-gifs.py [scene ...]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
# scene: (output file, width in pixels, frames per second)
SCENES = {"widget": ("widget.gif", 440, 20), "orb": ("orb.gif", 450, 30), "drop": ("drop-connect.gif", 880, 15)}


def timeline(shots: list[dict], fps: int, tail: float = 0.6) -> tuple[list[str], list[float]]:
    """The frame shown at each tick of a steady clock, with repeats merged into longer durations."""
    files: list[str] = []
    durations: list[float] = []
    step, index, t = 1 / fps, 0, shots[0]["t"]
    while t <= shots[-1]["t"] + tail:
        while index + 1 < len(shots) and shots[index + 1]["t"] <= t:
            index += 1
        if files and files[-1] == shots[index]["file"]:
            durations[-1] += step
        else:
            files.append(shots[index]["file"])
            durations.append(step)
        t += step
    return files, durations


def make_gif(scene: str) -> Path:
    target_name, width, fps = SCENES[scene]
    folder = ROOT / ".demo-media" / scene
    files, durations = timeline(json.loads((folder / "frames.json").read_text())["frames"], fps)
    cache: dict[str, Image.Image] = {}
    for file in files:
        if file not in cache:
            image = Image.open(folder / file).convert("RGB")
            cache[file] = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    images = [cache[file] for file in files]
    sample = images[:: max(1, len(images) // 14)]
    mosaic = Image.new("RGB", (width, images[0].height * len(sample)))
    for i, image in enumerate(sample):
        mosaic.paste(image, (0, i * image.height))
    palette = mosaic.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    frames = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
    target = ROOT / "docs" / "screenshots" / target_name
    frames[0].save(target, save_all=True, append_images=frames[1:], duration=[max(20, round(d * 1000)) for d in durations],
                   loop=0, optimize=True, disposal=1)
    print(f"{target.name}: {len(frames)} frames, {sum(durations):.1f} s, {target.stat().st_size / 1e6:.2f} MB")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenes", nargs="*", help=f"scenes to convert: {', '.join(SCENES)} (default: all)")
    scenes = parser.parse_args().scenes or list(SCENES)
    unknown = [scene for scene in scenes if scene not in SCENES]
    if unknown:
        parser.error(f"unknown scene: {', '.join(unknown)}")
    for scene in scenes:
        make_gif(scene)


if __name__ == "__main__":
    main()
