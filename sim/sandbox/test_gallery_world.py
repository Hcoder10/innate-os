#!/usr/bin/env python3
"""End-to-end check of the primitive-room path: build a world that is ONLY the
generated Gallery (no apartment meshes), through the real VirtualMars.

Verifies the things that fail silently:
  * a world with zero decomposed rooms still builds (statics-only)
  * the robot spawns inside the room, not embedded in a wall
  * dropped props land on the plinths they were aimed at, at the right height
  * the head camera sees something other than a void
"""

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"))

from mars_sim_driver.core import VirtualMars  # noqa: E402

FAIL = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        FAIL.append(label)


def main() -> int:
    print("building world (statics only)...")
    mars = VirtualMars(render_wh=(640, 480))
    print(f"  model: {mars.model.ngeom} geoms, {mars.model.nbody} bodies")

    print("\nrooms")
    check("gallery room loaded", "gallery" in mars.statics.rooms)
    room = mars.statics.rooms.get("gallery")
    check("geom count", room is not None and len(room.geoms) == 82, f"{len(room.geoms) if room else 0} (want 82)")

    print("\nspawn")
    x, y, yaw = mars.pose()
    check("inside the room", abs(x) < 4.3 and abs(y) < 4.3, f"({x:.2f}, {y:.2f}, {yaw:.0f} deg)")
    # The start pad is at the origin; the apartment default (-4.34) would be
    # buried in the west wall.
    check("on the start pad", abs(x) < 0.35 and abs(y) < 0.35, f"({x:.2f}, {y:.2f})")

    print("\nprops")
    names = [n for n in mars.props.props if n.startswith("gallery_")]
    check("16 gallery props registered", len(names) == 16, f"{len(names)}")

    # The ladder: each mug is aimed at the plinth built for it. Roblox z=-3.2
    # maps to MuJoCo y=+3.2, and x = -3 + (i-1)*1.5.
    ladder = [("gallery_mug_h00", -3.0, 0.0), ("gallery_mug_h10", -1.5, 0.10),
              ("gallery_mug_h20", 0.0, 0.20), ("gallery_mug_h30", 1.5, 0.30),
              ("gallery_mug_h50", 3.0, 0.50)]
    for name, mx, plinth_h in ladder:
        mars.drop_prop_at(name, mx, 3.2, 0.0)
    mars.step(2.0)  # let physics settle them onto the plinths

    poses = mars.object_poses()
    for name, mx, plinth_h in ladder:
        p = poses.get(name)
        if p is None:
            check(f"{name} landed", False, "not in object_poses")
            continue
        # rest_z for a mug is 0.0475 (half its body height above its own base).
        want_z = plinth_h + 0.0475
        settled = abs(p[2] - want_z) < 0.02 and abs(p[0] - mx) < 0.08 and abs(p[1] - 3.2) < 0.08
        check(f"{name} on its plinth", settled, f"z={p[2]:.3f} (want {want_z:.3f}) xy=({p[0]:.2f},{p[1]:.2f})")

    print("\ncamera")
    rgb = mars.render_rgb("main")  # CAMERAS is {"main", "wrist"}; there is no "head"
    check("head camera renders", rgb is not None and rgb.size > 0, f"{rgb.shape}")
    # A void renders as one flat colour; a room does not.
    check("sees geometry, not a void", float(np.std(rgb)) > 8.0, f"std={float(np.std(rgb)):.1f}")

    import PIL.Image

    PIL.Image.fromarray(rgb).save("/tmp/gallery_head.png")
    print("  wrote /tmp/gallery_head.png")

    print(f"\n{'ALL PASS' if not FAIL else str(len(FAIL)) + ' FAILED: ' + ', '.join(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
