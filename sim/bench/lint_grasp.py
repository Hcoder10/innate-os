#!/usr/bin/env python3
"""Does the object a challenge asks for actually FIT IN THE GRIPPER?

WHY THIS EXISTS. Every manipulation challenge in this suite failed at the lift,
on every map, on both the shim and Innate's own grasp stack -- while goal 1
(drive to the correctly-discriminated object) passed repeatedly. The floor
control was built to test whether surface height was the blocker; it failed
identically to its counter twin, which ruled that out and left no explanation.

The explanation was in the props. Measured on the cafe bundle:

    gripper, fully open   finger separation   0.082 m   (centre to centre)
    counter_cup_red       diameter            0.078 m
    counter_jar_jam       diameter            0.086 m

The jar is WIDER than the gripper opens. The cup clears the centre-to-centre
figure by 4 mm, which the fingers' own thickness eats. These objects cannot be
grasped by this robot at any position, on any surface, by any agent. Every
manipulation number the benchmark has produced measures the props I built, not
the robot -- and the robot's behaviour was RIGHT: it drove to the correct
object and then could not close on it.

`lint_reach` already checks that the robot can get close enough to an object it
must move. It never checked whether the object fits between the fingers once it
gets there, so the suite passed its own gates while asking for something
impossible. This is that missing gate.

THE MEASUREMENT. An object can be approached from any yaw, so what matters is
its NARROWEST horizontal cross-section -- a tall box 0.20 m x 0.05 m is grasped
across the 0.05 m axis. Cylinders and spheres have no narrow axis: their width
is the diameter whichever way you come at it.

Clear aperture is the finger separation at full open MINUS the finger
thickness, because the fingers are solid. A margin is then required on top:
a grasp that needs the object to be exactly aperture-wide is a grasp that
depends on millimetre positioning from a vision pipeline that reports pixels.

  usage: lint_grasp.py [bundle ...]      (default: every bundle)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GRASP_MARGIN_M = 0.008  # the object must be at least this much narrower


def measure(bundle: str) -> int:
    """Run in a CHILD process, one bundle at a time.

    core.py reads its asset dir at import, so a second bundle in the same
    interpreter is silently measured against the first one's world -- the
    same trap that made lint_reach report false clean maps for every bundle
    but the first.
    """
    sys.path.insert(0, str(REPO / "sim/sandbox"))
    sys.path.insert(0, str(REPO / "ros2_ws/src/mars_bot/mars_sim_driver"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _driver_pkg  # noqa: F401,PLC0415
    import mujoco  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    from capabilities import needs_move  # noqa: PLC0415
    from mars_sim_driver.challenges import load_challenges  # noqa: PLC0415
    from mars_sim_driver.core import VirtualMars  # noqa: PLC0415

    sim = VirtualMars()
    m, d = sim.model, sim.data

    # --- how wide does the gripper actually open? ---
    fingers = {}
    for j in range(m.njnt):
        name = mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_JOINT, j) or ""
        if name in ("robot_joint6", "robot_joint6M"):
            fingers[name] = j
    if len(fingers) != 2:
        print(f"  {bundle}: could not find both finger joints; skipping")
        return 0
    for j in fingers.values():
        lo, hi = m.jnt_range[j]
        d.qpos[m.jnt_qposadr[j]] = hi if hi > 0 else lo
    mujoco.mj_forward(m, d)

    sides = []
    thickness = 0.0
    for j in fingers.values():
        bid = m.jnt_bodyid[j]
        geoms = [g for g in range(m.ngeom) if m.geom_bodyid[g] == bid]
        sides.append([d.geom_xpos[g].copy() for g in geoms])
        for g in geoms:
            thickness = max(thickness, 2.0 * float(min(m.geom_size[g][:2])))
    separation = max(float(np.linalg.norm(a - b)) for a in sides[0] for b in sides[1])
    aperture = max(0.0, separation - thickness)

    def grasp_width(prop: str) -> float | None:
        """Narrowest horizontal width, the axis a gripper would close on."""
        try:
            bid = m.body(prop).id
        except Exception:  # noqa: BLE001
            return None
        widest = None
        for g in range(m.ngeom):
            if m.geom_bodyid[g] != bid:
                continue
            # COLLISION geoms only. Props carry a visual mesh alongside a
            # simple collision primitive, and the mesh is the decorative shape
            # -- contype=0, conaffinity=0, it touches nothing. Measuring it
            # reported the cafe cups as 135 mm when the body the fingers
            # actually meet is a 78 mm cylinder: the same verdict, arrived at
            # by measuring the wrong object.
            if int(m.geom_contype[g]) == 0 and int(m.geom_conaffinity[g]) == 0:
                continue
            gtype, size = int(m.geom_type[g]), m.geom_size[g]
            if gtype == mujoco.mjtGeom.mjGEOM_SPHERE:
                w = 2 * size[0]
            elif gtype in (mujoco.mjtGeom.mjGEOM_CYLINDER, mujoco.mjtGeom.mjGEOM_CAPSULE):
                w = 2 * size[0]  # no narrow axis: same width from every yaw
            elif gtype == mujoco.mjtGeom.mjGEOM_BOX:
                w = 2 * min(size[0], size[1])  # approach across the short side
            elif gtype == mujoco.mjtGeom.mjGEOM_MESH:
                w = 2 * min(size[0], size[1])  # mesh bbox half-extents
            else:
                continue
            # The widest part of the object is what has to fit.
            widest = w if widest is None else max(widest, w)
        return widest

    challenges = load_challenges([REPO / "sim/bundles" / bundle / "challenges"])
    print(f"=== {bundle}: gripper opens {separation:.3f} m centre-to-centre, "
          f"finger thickness {thickness:.3f} m -> clear aperture {aperture:.3f} m")

    problems = 0
    seen: set[str] = set()
    for cid, ch in sorted(challenges.items()):
        for drop in ch.setup:
            prop = getattr(drop, "name", None)
            if not prop or not needs_move(ch, prop) or (cid, prop) in seen:
                continue
            seen.add((cid, prop))
            width = grasp_width(prop)
            if width is None:
                continue
            if width > aperture - GRASP_MARGIN_M:
                verdict = "IMPOSSIBLE" if width > aperture else "no margin"
                print(f"  GRASP  {cid}: {prop} is {width * 1000:.0f} mm across, "
                      f"aperture is {aperture * 1000:.0f} mm -- {verdict}")
                print(f"ROW\t{cid}\t{prop}\t{width:.4f}\t{aperture:.4f}")
                problems += 1
    if not problems:
        print("  clean")
    return problems


MANIFEST = Path(__file__).resolve().parent / "ungraspable.json"


def write_manifest(rows: list[tuple[str, str, float, float]]) -> None:
    """Record which challenges ask for something that does not fit the gripper.

    The capability gate reads this. A challenge whose target is physically
    ungraspable is not a challenge the agent failed -- it is one the harness
    should never have asked, and scoring it 0 attributes my prop dimensions to
    the robot. Written as a file rather than computed at gate time because the
    measurement needs MuJoCo and a loaded world per map, which is far too heavy
    to do before every run.
    """
    import json  # noqa: PLC0415

    payload = {
        cid: f"{prop} is {w * 1000:.0f} mm across; the gripper's clear aperture is {ap * 1000:.0f} mm"
        for cid, prop, w, ap in rows
    }
    MANIFEST.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    print(f"wrote {MANIFEST.name}: {len(payload)} challenge(s) with an ungraspable target")


def main() -> int:
    write = "--manifest" in sys.argv[1:]
    bundles = [a for a in sys.argv[1:] if a != "--manifest"]
    if bundles:
        if len(bundles) == 1 and os.environ.get("_LINT_GRASP_CHILD"):
            return measure(bundles[0])
        names = bundles
    else:
        names = sorted(p.name for p in (REPO / "sim/bundles").iterdir() if p.is_dir())

    total = 0
    rows: list[tuple[str, str, float, float]] = []
    for name in names:
        env = {**os.environ, "_LINT_GRASP_CHILD": "1",
               "VIRTUAL_MARS_ASSETS": str(REPO / "sim/bundles" / name),
               "MUJOCO_GL": "osmesa"}
        proc = subprocess.run([sys.executable, __file__, name], capture_output=True,
                              text=True, env=env)
        for line in proc.stdout.splitlines():
            if line.startswith("ROW\t"):
                _, cid, prop, w, ap = line.split("\t")
                rows.append((cid, prop, float(w), float(ap)))
            elif not line.startswith(("[props]", "[rooms]", "[challenges]")):
                print(line)
        total += proc.returncode
    print(f"\n{total} ungraspable target(s) across {len(names)} map(s)")
    if write:
        write_manifest(rows)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
