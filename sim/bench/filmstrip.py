#!/usr/bin/env python3
"""Turn a recorded run into something a person can look at.

WHY NOT THE WEB VIEWER. The webapp's 3D view renders a fixed apartment asset
bundle baked into the image; it does not follow VIRTUAL_MARS_ASSETS. Watching it
during a cafe episode shows a robot walking around an apartment it is not in --
worse than no view, because it looks authoritative. The robot's camera is the
honest picture: MuJoCo renders it from the world that is actually loaded, and it
is the exact image the brain reasons over.

record_run.py writes those frames as NNNN_t<elapsed>_x<x>_y<y>.jpg. This turns a
directory of them into two artefacts:

  * a contact sheet -- one image, N frames on a grid, each captioned with its
    timestamp and the robot's pose, for a glance at the whole episode
  * an animated GIF -- the run played back, for watching it move

Both are captioned from the FILENAMES, so the pose under a frame is the pose
recorded with it rather than something re-derived here.

  usage: filmstrip.py <frames_dir> <out_prefix> [tiles]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

STAMP = re.compile(r"^(\d+)_t([\d.]+)_x([-+][\d.]+)_y([-+][\d.]+)\.jpg$")
BAR_H = 22  # caption strip drawn under each tile
GIF_MS = 200  # 5 fps playback; frames were sampled at 1 Hz, so 5x real time


def _caption(path: Path) -> str:
    m = STAMP.match(path.name)
    if not m:
        return path.stem
    _, t, x, y = m.groups()
    return f"t={float(t):5.1f}s   pose ({x}, {y})"


def _label(im: Image.Image, text: str) -> Image.Image:
    """Frame plus a caption strip underneath. Drawn below rather than over the
    image so the caption never hides what the robot was looking at."""
    out = Image.new("RGB", (im.width, im.height + BAR_H), (18, 18, 22))
    out.paste(im.convert("RGB"), (0, 0))
    ImageDraw.Draw(out).text((6, im.height + 5), text, fill=(215, 215, 225))
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    frames_dir = Path(sys.argv[1])
    prefix = Path(sys.argv[2])
    tiles = int(sys.argv[3]) if len(sys.argv) > 3 else 12

    shots = sorted(frames_dir.glob("*.jpg"))
    if not shots:
        print(f"no frames in {frames_dir}")
        return 1

    # Evenly spaced across the episode rather than the first N, so a long run
    # does not produce a contact sheet of its opening seconds.
    step = max(1, len(shots) // tiles)
    picked = shots[::step][:tiles]

    labelled = [_label(Image.open(p), _caption(p)) for p in picked]
    w, h = labelled[0].size
    cols = 4 if len(labelled) >= 4 else len(labelled)
    rows = (len(labelled) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * h), (18, 18, 22))
    for i, tile in enumerate(labelled):
        sheet.paste(tile, ((i % cols) * w, (i // cols) * h))
    sheet_path = prefix.with_name(prefix.name + "_sheet.jpg")
    sheet.save(sheet_path, quality=88)

    # The GIF uses every frame, not just the tiles: the point of it is motion.
    movie = [_label(Image.open(p), _caption(p)) for p in shots]
    half = (movie[0].width // 2, movie[0].height // 2)
    movie = [f.resize(half) for f in movie]
    gif_path = prefix.with_name(prefix.name + ".gif")
    movie[0].save(
        gif_path,
        save_all=True,
        append_images=movie[1:],
        duration=GIF_MS,
        loop=0,
        optimize=True,
    )

    print(f"{len(shots)} frames over {_caption(shots[-1])}")
    print(f"  sheet -> {sheet_path}  ({sheet.size[0]}x{sheet.size[1]}, {len(picked)} tiles)")
    print(f"  gif   -> {gif_path}  ({gif_path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
