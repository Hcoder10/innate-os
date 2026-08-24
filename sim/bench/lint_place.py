#!/usr/bin/env python3
"""Can the robot PUT THE OBJECT DOWN where the goal says?

`lint_reach` checks the pickup: is there a cell the robot can stand in within
arm's reach of the Drop. It never checks the other half. A fetch challenge has
two physical requirements and the suite only gated one:

  1. reach the object          -- lint_reach
  2. fit it in the gripper     -- lint_grasp
  3. reach the DESTINATION     -- here
  4. and the destination must be within the arm's VERTICAL envelope -- here

Point 4 is the one that hides. Every position in the challenge DSL is (x, y);
height comes from whatever the object lands on. So a goal that reads
`InCircle("counter_jar_jam", 0.0, 1.30, 0.45)` looks like a floor coordinate and
is actually the top of a counter. The arm works below roughly 0.30 m and the
base has no vertical freedom at all (`add_planar_base` gives x, y and yaw), so
a destination on a 0.75 m worktop cannot be satisfied by this robot at any
position -- and the challenge still passes every other gate, because the oracle
that proves solvability teleports props and never moves an arm.

HEIGHT IS MEASURED, NOT ASSUMED: a ray is cast straight down at the
destination and the first surface it hits is the height the object would have
to be placed at.

  usage: lint_place.py [bundle ...]      (default: every bundle)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ARM_REACH_M = 0.29   # same figure lint_reach uses, past the base
ARM_Z_MAX_M = 0.30   # the arm works below roughly this; the base cannot climb
SETTLE_STEPS = 6000  # 12 s at the 2 ms timestep; these scenes are not still at 0.8 s
DRIFT_LIMIT_M = 0.03 # further than this and the object did not stay where it was put


def measure(bundle: str) -> int:
    sys.path.insert(0, str(REPO / "sim/sandbox"))
    sys.path.insert(0, str(REPO / "ros2_ws/src/mars_bot/mars_sim_driver"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _driver_pkg  # noqa: F401,PLC0415
    import mujoco  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    from capabilities import needs_move  # noqa: PLC0415
    from mars_sim_driver.challenges import InCircle, InRect, load_challenges  # noqa: PLC0415
    from mars_sim_driver.core import VirtualMars  # noqa: PLC0415

    sim = VirtualMars()
    m, d = sim.model, sim.data
    # Park the robot far away so its own body is not what the ray hits.
    saved = d.qpos.copy()
    d.qpos[sim._base["x"][0]] += 100.0
    mujoco.mj_forward(m, d)

    def settle(prop: str, x: float, y: float) -> tuple[float, float] | None:
        """(resting height, horizontal drift) after the object has actually stopped.

        RESETS FIRST. Without it every measured drop stayed on the floor for
        the rest of the process and later drops landed on top of earlier ones:
        measured errors up to 62.7 mm, and every blaze PLACE goal shares one
        porch coordinate so the props stacked.

        SETTLES FOR 12 s, NOT 0.8 s. The first version stepped 400 times and
        read the height, which is long before these scenes are still. A carton
        sat perched at 31.0 cm for 1.6 s and then dropped to 28.5 cm -- so the
        lint reported two impossible placements that were fine. In the other
        direction a documents box read a clean 18 cm at 0.8 s and was on the
        floor 40 cm away by 12 s, which the lint called clean. Velocity is no
        help: that box reads |v| = 0.0000 while perched.

        DRIFT IS RETURNED because "it fell over" is the failure that actually
        bit this benchmark -- a uniform prop rescale shrank a footprint until
        it no longer bridged its shelf, and nothing here noticed.
        """
        sim.reset()
        if not sim.props.drop_at(d, prop, x, y):
            return None
        for _ in range(SETTLE_STEPS):
            mujoco.mj_step(m, d)
        try:
            pos = d.xpos[m.body(prop).id]
        except Exception:  # noqa: BLE001
            return None
        return float(pos[2]), float(np.hypot(pos[0] - x, pos[1] - y))

    grid, ox, oy = sim.occupancy_grid(0.05)
    free = grid == 0

    def standable_within(x: float, y: float, reach: float) -> bool:
        """Is there any free cell within `reach` of (x, y)?"""
        rows, cols = np.nonzero(free)
        xs = ox + (cols + 0.5) * 0.05
        ys = oy + (rows + 0.5) * 0.05
        return bool(np.any(np.hypot(xs - x, ys - y) <= reach))

    challenges = load_challenges([REPO / "sim/bundles" / bundle / "challenges"])
    print(f"=== {bundle}: arm reaches {ARM_REACH_M:.2f} m out, {ARM_Z_MAX_M:.2f} m up")
    problems = 0

    for cid, ch in sorted(challenges.items()):
        for goal in ch.goals:
            pred = getattr(goal, "predicate", goal)
            inner = getattr(pred, "inner", pred)
            if isinstance(inner, InCircle):
                target, gx, gy = inner.target, inner.x, inner.y
                tolerance = inner.radius_m
            elif isinstance(inner, InRect):
                target = inner.target
                gx, gy = (inner.x0 + inner.x1) / 2, (inner.y0 + inner.y1) / 2
                tolerance = min(abs(inner.x1 - inner.x0), abs(inner.y1 - inner.y0)) / 2
            else:
                continue
            # Only destinations for objects the robot must CARRY. A goal on the
            # robot is a place to stand, which lint_reach already checks.
            if target == "robot" or not needs_move(ch, target):
                continue

            settled = settle(target, gx, gy)
            z, drift = settled if settled else (None, 0.0)
            if z is not None and z > ARM_Z_MAX_M:
                print(f"  PLACE  {cid}: {target} would rest {z * 100:.0f} cm up at "
                      f"({gx:+.2f}, {gy:+.2f}) -- arm tops out at {ARM_Z_MAX_M * 100:.0f} cm")
                problems += 1
            if not standable_within(gx, gy, ARM_REACH_M):
                print(f"  PLACE  {cid}: no standable cell within {ARM_REACH_M:.2f} m of "
                      f"the destination ({gx:+.2f}, {gy:+.2f})")
                problems += 1
            # Judged against the GOAL'S OWN tolerance, not a fixed number. A
            # mug that rolls 6 cm inside a 30 cm delivery circle still satisfies
            # the goal; flagging it reports a problem the run will never have.
            # What matters is whether it leaves the circle it was put in.
            if drift > tolerance:
                print(f"  PLACE  {cid}: {target} slides {drift * 100:.0f} cm after being put "
                      f"down at ({gx:+.2f}, {gy:+.2f}) -- outside the goal's own "
                      f"{tolerance * 100:.0f} cm tolerance")
                problems += 1

        # And the pickup height, which lint_reach does not look at either.
        for drop in ch.setup:
            prop = getattr(drop, "name", None)
            if not prop or not needs_move(ch, prop):
                continue
            settled = settle(prop, drop.x, drop.y)
            z, drift = settled if settled else (None, 0.0)
            if z is not None and z > ARM_Z_MAX_M:
                print(f"  PICK   {cid}: {prop} rests {z * 100:.0f} cm up at "
                      f"({drop.x:+.2f}, {drop.y:+.2f}) -- arm tops out at "
                      f"{ARM_Z_MAX_M * 100:.0f} cm")
                problems += 1
            # THE ONE THAT ACTUALLY BIT. Rescaling blaze_documents to fit the
            # gripper shrank its footprint until it no longer bridged its
            # shelf: it sat still for 0.8 s, then slid 40 cm onto the floor.
            # The agent would have arrived to find nothing where the brief said
            # it was, and every gate called the scene clean.
            if drift > DRIFT_LIMIT_M:
                print(f"  PICK   {cid}: {prop} does not stay put -- it moves "
                      f"{drift * 100:.0f} cm from ({drop.x:+.2f}, {drop.y:+.2f}) "
                      f"within {SETTLE_STEPS * 0.002:.0f}s of being dropped")
                problems += 1

    d.qpos[:] = saved
    mujoco.mj_forward(m, d)
    if not problems:
        print("  clean")
    return problems


def main() -> int:
    args = sys.argv[1:]
    if args and os.environ.get("_LINT_PLACE_CHILD"):
        return measure(args[0])
    names = args or sorted(p.name for p in (REPO / "sim/bundles").iterdir() if p.is_dir())
    total = 0
    for name in names:
        env = {**os.environ, "_LINT_PLACE_CHILD": "1",
               "VIRTUAL_MARS_ASSETS": str(REPO / "sim/bundles" / name),
               "MUJOCO_GL": "osmesa"}
        proc = subprocess.run([sys.executable, __file__, name], capture_output=True,
                              text=True, env=env)
        for line in proc.stdout.splitlines():
            if not line.startswith(("[props]", "[rooms]", "[challenges]")):
                print(line)
        if proc.returncode and not proc.stdout.strip():
            print(f"  ERROR in {name}: {proc.stderr.strip()[-200:]}")
        total += proc.returncode
    print(f"\n{total} placement problem(s) across {len(names)} map(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
