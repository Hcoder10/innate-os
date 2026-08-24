#!/usr/bin/env python3
"""Run an episode and dump the full robot state at the moment a leg wedges.

Written for the household deadlock: the robot stops on floor the occupancy grid
says is free, with a clear target half a metre ahead. Everything cheap has
already been ruled out (regression, grid, props, derived waypoints), so this
looks at the only thing left -- what MuJoCo itself thinks is happening.

Prints, once the base has moved less than a centimetre for a few seconds:

  * commanded velocity vs the base joints' ACTUAL velocity. A large gap means
    something is holding it; no gap means the controller stopped asking.
  * every contact involving a robot geom, with the penetration depth and the
    normal force. This is the direct question "what is it touching".
  * the actuator forces on base_x / base_y / base_yaw, so a base that is
    pushing at full effort and not moving is distinguishable from one that is
    not pushing.

  ./sim/.venv/bin/python sim/bench/diag_stall.py household household_tour
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

CONTROL_DT = 0.05


def main() -> int:
    map_name = sys.argv[1] if len(sys.argv) > 1 else "household"
    challenge_id = sys.argv[2] if len(sys.argv) > 2 else "household_tour"

    import mujoco
    import numpy as np

    import autoplan
    from oracles import ORACLES
    from planner_agent import PlannerAgent
    from runner import sources

    assets, ch_root = sources()[map_name]
    if assets is not None:
        os.environ["VIRTUAL_MARS_ASSETS"] = str(assets)

    from mars_sim_driver.challenges import ChallengeEngine, load_challenges
    from mars_sim_driver.core import VirtualMars

    ch = load_challenges([ch_root])[challenge_id]
    mars = VirtualMars(render_wh=(160, 120))
    import threading

    engine = ChallengeEngine(mars, threading.Lock(), roots=[ch_root],
                             progress_path=BENCH / "results" / "progress" / "diag.json")

    from navplan import NavMap

    nav = NavMap.from_sim(mars)
    steps = ORACLES.get(challenge_id) or autoplan.plan_for(ch)
    agent = PlannerAgent(steps)
    engine.start(challenge_id)
    agent.reset(mars, ch, nav=nav)

    m, d = mars.model, mars.data
    jid = {n: mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, n) for n in ("base_x", "base_y", "base_yaw")}
    qadr = {n: m.jnt_qposadr[i] for n, i in jid.items() if i >= 0}
    vadr = {n: m.jnt_dofadr[i] for n, i in jid.items() if i >= 0}

    def gname(g):
        return mujoco.mj_id2name(m, mujoco.mjtObj.mjOBJ_GEOM, int(g)) or f"geom{g}"

    def bname(g):
        return m.body(int(m.geom_bodyid[g])).name or "?"

    t0 = float(d.time)
    last_xy, still_since = None, None
    for i in range(24000):
        t = float(d.time) - t0
        agent.act(mars, t)
        mars.step(CONTROL_DT)
        x, y, yaw = mars.pose()

        if last_xy is None or math.hypot(x - last_xy[0], y - last_xy[1]) > 0.01:
            last_xy, still_since = (x, y), t
            continue
        if t - still_since < 4.0:
            continue

        # --- wedged. Dump everything. ---
        step = agent.steps[agent.i] if agent.i < len(agent.steps) else ("done",)
        print(f"\nWEDGED after {t:.1f}s of sim, still for {t - still_since:.1f}s")
        print(f"  pose      ({x:.3f}, {y:.3f})  yaw {math.degrees(yaw):.1f} deg")
        print(f"  plan step {agent.i}: {step}")
        if agent._path:
            wp = agent._path[agent._wp]
            print(f"  waypoint  {agent._wp + 1}/{len(agent._path)} = ({wp[0]:.2f}, {wp[1]:.2f})"
                  f"  {math.hypot(wp[0] - x, wp[1] - y):.2f} m away")

        print("\n  base joints          qpos        qvel     actuator force")
        for n in ("base_x", "base_y", "base_yaw"):
            if n not in qadr:
                continue
            aid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_ACTUATOR, n)
            f = float(d.actuator_force[aid]) if aid >= 0 else float("nan")
            print(f"    {n:<10} {float(d.qpos[qadr[n]]):10.4f} {float(d.qvel[vadr[n]]):10.4f} {f:14.4f}")

        print(f"\n  contacts involving the robot ({d.ncon} total in the world):")
        forces = np.zeros(6)
        rows = []
        for k in range(d.ncon):
            c = d.contact[k]
            b1, b2 = bname(c.geom1), bname(c.geom2)
            if not (b1.startswith("robot") or b2.startswith("robot")):
                continue
            mujoco.mj_contactForce(m, d, k, forces)
            rows.append((abs(float(forces[0])), b1, gname(c.geom1), b2, gname(c.geom2), float(c.dist)))
        if not rows:
            print("    NONE -- nothing is touching the robot at all")
        for fn, b1, g1, b2, g2, dist in sorted(rows, reverse=True)[:12]:
            print(f"    |F|={fn:8.2f} N  depth={-dist * 1000:6.2f} mm   {b1}/{g1}  <->  {b2}/{g2}")

        print("\n  arm joint positions:")
        for n, v in sorted(mars.joint_positions().items()):
            print(f"    {n:<24} {v:8.4f}")
        return 0

    print("no wedge detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
