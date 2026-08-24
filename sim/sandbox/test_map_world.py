#!/usr/bin/env python3
"""Build one generated map bundle through the real VirtualMars and check it.

Usage: VIRTUAL_MARS_ASSETS=sim/bundles/<map> test_map_world.py <map>

ASSETS_DIR is read once at import, so each bundle needs its own process --
looping inside one would silently keep testing the first map.

Checks the things that fail silently rather than loudly:
  * a statics-only world compiles at all (no apartment meshes in the bundle)
  * the robot spawns in free space -- if it starts inside a wall, physics ejects
    it and every later result is measured from the wrong place
  * every prop sidecar in the bundle actually reached the model
  * the camera sees geometry rather than a void
"""

import math
import os
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"))

from mars_sim_driver.core import VirtualMars  # noqa: E402

FAIL = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAIL.append(label)


def main() -> int:
    name = sys.argv[1]
    bundle = Path(os.environ["VIRTUAL_MARS_ASSETS"])
    print(f"=== {name} ===")

    mars = VirtualMars(render_wh=(640, 480))
    print(f"  model: {mars.model.ngeom} geoms, {mars.model.nbody} bodies")

    room = mars.statics.rooms.get(name)
    check("room loaded", room is not None)
    if room is None:
        return 1
    check("has geometry", len(room.geoms) > 20, f"{len(room.geoms)} geoms")

    # Spawn must be inside the room's own xy footprint.
    xs = [g.pos[0] for g in room.geoms]
    ys = [g.pos[1] for g in room.geoms]
    x, y, yaw = mars.pose()
    inside = min(xs) - 0.5 < x < max(xs) + 0.5 and min(ys) - 0.5 < y < max(ys) + 0.5
    check("spawn inside footprint", inside, f"({x:.2f}, {y:.2f}) in x[{min(xs):.1f},{max(xs):.1f}] y[{min(ys):.1f},{max(ys):.1f}]")

    # Spawned inside a wall? Physics would shove the base out. Settle and see if
    # it stayed put.
    sx, sy = x, y
    mars.step(0.6)
    x2, y2, yaw2 = mars.pose()
    drift = math.hypot(x2 - sx, y2 - sy)
    check("spawn is clear (no ejection)", drift < 0.05, f"drift {drift * 1000:.0f} mm")

    # Not ejected is NOT the same as free to move: the robot used to spawn
    # inside a solid StartPad slab, pinned by a penetrating contact. That shows
    # zero drift too, so drift alone called it clear while the base could not
    # turn at all. Command a spin and confirm it actually turns.
    # cmd_vel expires after CMD_VEL_TIMEOUT_S = 0.5, so re-send every tick.
    for _ in range(30):
        mars.set_cmd_vel(0.0, 1.0)
        mars.step(0.05)
    turned = abs((mars.pose()[2] - yaw2 + math.pi) % (2 * math.pi) - math.pi)
    check("robot can move from spawn", turned > 0.5, f"turned {math.degrees(turned):.0f} deg in 1.5 s")
    mars.set_cmd_vel(0.0, 0.0)
    mars.reset()

    want = len([p for p in (bundle / "props").glob("*.py") if not p.name.startswith("_")])
    got = [n for n in mars.props.props if n.startswith(f"{name}_")]
    check("every prop sidecar loaded", len(got) == want, f"{len(got)}/{want}")

    rgb = mars.render_rgb("main")
    check("camera renders", rgb is not None and rgb.size > 0, f"{rgb.shape}")
    check("sees geometry, not a void", float(np.std(rgb)) > 8.0, f"std={float(np.std(rgb)):.1f}")

    import PIL.Image

    out = f"/tmp/map_{name}.png"
    PIL.Image.fromarray(rgb).save(out)
    print(f"  wrote {out}")

    print(f"  {'ALL PASS' if not FAIL else str(len(FAIL)) + ' FAILED'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
