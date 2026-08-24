#!/usr/bin/env python3
"""Make every map's walls greyer than its floor.

WHY GEOMETRY, NOT NAMES. The obvious approach -- recolour geoms named wall*/
side* -- paints the wrong things and misses the right ones. In counter,
`sidee/siden/sides/sidew` are the four sides of a 14 cm planter box, not
walls; in blaze the actual walls are `perimn_1`, `halls_2`, `divn_1` --
digit-suffixed names an [a-z_]+ pattern silently drops. So a wall is defined
here by what a wall IS in these maps: a collidable box, tall (half-height
>= 0.3 m), thin on one horizontal axis (<= 0.12 m half-size), standing at
wall height (centre z >= 0.3 m). Furniture legs are thin but not tall-and-
long; doors and headers ride along only if they are wall-shaped, which for a
recolour is correct.

THE COLOUR. One neutral grey, (0.58, 0.58, 0.58), for every wall in every
map. Every floor in the suite is either saturated wood/brown or far from
mid-grey in value (0.796 warm light, 0.290 dark slate), so this grey is
"greyer than the floor" on both axes -- lower saturation than the wood, and
separated in value from both grey-ish floors -- without inventing a per-map
palette.

HONESTY NOTE, recorded here because it belongs next to the change: this
alters the visual scene every camera-based result was measured against
(counter_read_the_pass explicitly calibrates cup silhouettes against the
back wall). Scores taken before and after this change are not comparable on
visual challenges, and the run log records which side of the change a sweep
was on.

Usage:
    recolor_walls.py           # dry run: list what would change, per bundle
    recolor_walls.py --apply   # rewrite the rooms files in place
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BUNDLES = Path(__file__).resolve().parents[1] / "bundles"

WALL_GREY = (0.58, 0.58, 0.58)
HALF_Z_MIN = 0.3     # tall: >= 0.6 m full height
THIN_MAX = 0.12      # thin on one horizontal axis
CENTRE_Z_MIN = 0.3   # standing at wall height, not a tall skirting at z~0

# Wall-SHAPED but not walls, verified by colour and role before excluding:
#   jamb_*    rounds' door frames, painted red/blue/green/yellow -- the door
#             colours are semantic ("the room with the blue door") and
#             greying them would break find_bathroom and all_doors.
#   gate*     bridge's five gates: the task structure itself. Left brown --
#             grey walls behind brown gates makes the gates MORE legible.
#   upright   furniture wood (shelf/bench uprights), same stain as the rest
#             of the furniture it belongs to.
EXCLUDE = re.compile(r"^(jamb_|gate\d|upright)")

GEOM = re.compile(
    r'Geom\("box", \(([^)]+)\), \(([^)]+)\), quat=\([^)]+\), '
    r'rgba=\(([^)]+)\), name="([a-zA-Z0-9_]+)"(, collide=False)?\)'
)


def is_wall(name: str, sx: float, sy: float, sz: float, z: float, collide: bool) -> bool:
    if EXCLUDE.match(name):
        return False
    return collide and sz >= HALF_Z_MIN and min(sx, sy) <= THIN_MAX and z >= CENTRE_Z_MIN


def main() -> int:
    apply = "--apply" in sys.argv
    total = 0
    for f in sorted(BUNDLES.glob("*/rooms/*.py")):
        bundle = f.parent.parent.name
        text = f.read_text()
        hits: dict[str, int] = {}

        def sub(m: re.Match) -> str:
            sx, sy, sz = (float(v) for v in m.group(1).split(","))
            z = float(m.group(2).split(",")[2])
            if not is_wall(m.group(4), sx, sy, sz, z, m.group(5) is None):
                return m.group(0)
            hits[m.group(4)] = hits.get(m.group(4), 0) + 1
            old_rgba = m.group(3)
            alpha = old_rgba.split(",")[3].strip()
            new_rgba = f"{WALL_GREY[0]}, {WALL_GREY[1]}, {WALL_GREY[2]}, {alpha}"
            return m.group(0).replace(f"rgba=({old_rgba})", f"rgba=({new_rgba})")

        new_text = GEOM.sub(sub, text)
        n = sum(hits.values())
        total += n
        names = ", ".join(f"{k}x{v}" if v > 1 else k for k, v in sorted(hits.items()))
        print(f"{bundle:10s} {n:3d} wall geoms  {names}")
        if apply and n:
            f.write_text(new_text)
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {total} geoms across all bundles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
