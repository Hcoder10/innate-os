#!/usr/bin/env python3
"""Check every challenge's Drops and goal points against what the robot can do.

WHY THIS EXISTS. The same defect has now been found three times, on three maps,
each time by a different and more expensive route:

  * the cafe counter was 0.60 m deep, so the cups sat past the arm's envelope;
    caught by A* reporting "no path" and the gate refusing the challenge;
  * the household mug sat 0.51 m from the nearest cell the robot can occupy;
    caught by an oracle that ran its whole plan and satisfied no goal;
  * household station pads were 12 mm of collidable floor paint; caught only by
    dumping MuJoCo contacts after a quarter-hour deadlock.

All three are one question -- can the robot physically get to the thing -- and
none of them needs an episode to answer. This answers it in a few seconds per
map, before anything is run.

WHAT IT CHECKS

  reach   For every Drop, the distance from the object to the nearest cell the
          robot can stand in. If that exceeds ARM_REACH_M the object cannot be
          picked up by any agent, however good.
  goals   For every InCircle/InRect goal on the ROBOT, whether its region
          contains a free cell at all. A goal region entirely inside furniture
          is unsatisfiable.
  paint   Any collidable geom in the room whose top is under 3 cm. A planar
          base cannot climb, so floor markings that collide are walls -- and
          they are almost always meant to be paint.

WHAT IT DELIBERATELY DOES NOT DO. It does not fail the build. Some of these are
intentional: counter_out_of_reach places a teapot beyond the arm ON PURPOSE, and
that is the whole challenge. The lint reports; a person decides.

  ./sim/.venv/bin/python sim/bench/lint_reach.py            # every map
  ./sim/.venv/bin/python sim/bench/lint_reach.py counter
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parents[1]
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"))
os.environ.setdefault("MUJOCO_GL", "osmesa")

# How far the gripper reaches past the base centre. From sim/props/11_can.py's
# tuned reach=(0.296, 0.011), rounded down: a check that uses the optimistic
# number passes objects the arm only just misses.
ARM_REACH_M = 0.29
# A collidable geom lower than this is almost certainly meant to be paint.
PAINT_TOP_M = 0.03


def check_map(name: str) -> int:
    import mujoco

    from mars_sim_driver.challenges import Drop, InCircle, InRect, load_challenges
    from navplan import NavMap
    from runner import sources

    assets, ch_root = sources()[name]
    if assets is not None:
        os.environ["VIRTUAL_MARS_ASSETS"] = str(assets)
    else:
        os.environ.pop("VIRTUAL_MARS_ASSETS", None)

    from mars_sim_driver.core import VirtualMars

    mars = VirtualMars(render_wh=(160, 120))
    nav = NavMap.from_sim(mars)
    challenges = load_challenges([ch_root])
    problems = 0

    # --- paint that collides -------------------------------------------------
    m = mars.model
    paint = []
    for gid in range(m.ngeom):
        body = m.body(int(m.geom_bodyid[gid])).name or ""
        if not body.startswith("room_"):
            continue
        if m.geom_contype[gid] == 0 and m.geom_conaffinity[gid] == 0:
            continue
        top = float(mars.data.geom_xpos[gid][2] + abs(m.geom_size[gid]).max())
        if 0.001 < top < PAINT_TOP_M:
            gname = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, gid) or f"geom{gid}"
            paint.append((gname, top))
    if paint:
        problems += len(paint)
        print(f"  PAINT  {len(paint)} collidable geom(s) under {PAINT_TOP_M * 100:.0f} cm "
              f"-- a planar base cannot climb these:")
        for gname, top in sorted(paint)[:8]:
            print(f"           {gname:<34} top {top * 1000:5.1f} mm")
        if len(paint) > 8:
            print(f"           ... and {len(paint) - 8} more")

    # --- drops out of reach --------------------------------------------------
    # "Does this object have to END UP somewhere?" lives in capabilities.py,
    # because the capability gate asks the identical question -- it needs to
    # know whether a challenge requires a pick at all, this needs to know
    # whether a particular prop must be reachable. Two implementations of a
    # rule with this many edge cases (Hold is a duration wrapper, not a grasp;
    # a placement the setup already satisfies is a stay-put goal) would drift,
    # and the copy that drifted silently would be the one shipping numbers.
    from capabilities import needs_move as must_move  # noqa: PLC0415

    for cid, ch in sorted(challenges.items()):
        for drop in ch.setup:
            if not isinstance(drop, Drop):
                continue
            if not must_move(ch, drop.name):
                continue
            cell = nav.nearest_free(drop.x, drop.y, 2.0)
            if cell is None:
                print(f"  REACH  {cid}: {drop.name} at ({drop.x:.2f}, {drop.y:.2f}) "
                      f"has NO free cell within 2 m")
                problems += 1
                continue
            fx, fy = nav.to_world(*cell)
            d = math.hypot(fx - drop.x, fy - drop.y)
            if d > ARM_REACH_M:
                print(f"  REACH  {cid}: {drop.name} at ({drop.x:.2f}, {drop.y:.2f}) is "
                      f"{d:.2f} m from the nearest standable cell (arm reaches {ARM_REACH_M:.2f})")
                problems += 1

        # --- robot goal regions with no floor in them ------------------------
        for goal in ch.goals:
            p = goal.predicate
            inner = getattr(p, "inner", p)
            if isinstance(inner, InCircle) and inner.target == "robot":
                cell = nav.nearest_free(inner.x, inner.y, inner.radius_m)
                if cell is None:
                    print(f"  GOAL   {cid}: {goal.label!r} -- no standable cell within "
                          f"{inner.radius_m:.2f} m of ({inner.x:.2f}, {inner.y:.2f})")
                    problems += 1
            elif isinstance(inner, InRect) and inner.target == "robot":
                cx, cy = (inner.x0 + inner.x1) / 2, (inner.y0 + inner.y1) / 2
                half = max(abs(inner.x1 - inner.x0), abs(inner.y1 - inner.y0)) / 2
                if nav.nearest_free(cx, cy, half) is None:
                    print(f"  GOAL   {cid}: {goal.label!r} -- rect contains no standable cell")
                    problems += 1
    return problems


def _one_map(name: str) -> int:
    print(f"\n=== {name}")
    try:
        n = check_map(name)
    except Exception as exc:  # noqa: BLE001 -- one bad map must not stop the lint
        print(f"  ERROR  {type(exc).__name__}: {exc}")
        n = 1
    if n == 0:
        print("  clean")
    return n


def main() -> int:
    import subprocess

    from runner import sources

    names = sys.argv[1:] or [n for n in sources() if n != "apartment"]

    # ONE MAP PER PROCESS. core.py reads ASSETS_DIR at import time, so a second
    # map in the same interpreter is silently measured against the first map's
    # world -- which produced FALSE CLEAN REPORTS for every map but the first,
    # in the very tool built to catch geometry faults. See CHANGES.md (patch_lintfork).
    if len(names) > 1:
        total = 0
        for name in names:
            proc = subprocess.run([sys.executable, __file__, name], capture_output=True, text=True)
            body = "\n".join(l for l in proc.stdout.splitlines()
                              if not l.startswith(("[props]", "[rooms]", "[challenges]")))
            print(body)
            # The child prints its own count; re-derive from its findings so a
            # crashed child cannot silently contribute zero.
            total += sum(1 for l in body.splitlines() if l.strip().startswith(("REACH", "GOAL", "PAINT", "ERROR")))
        print(f"\n{total} thing(s) to look at across {len(names)} map(s)")
        return 0

    total = _one_map(names[0])
    print(f"\n{total} thing(s) to look at across 1 map(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
