"""Headless divergence/instability check for whatever's currently under
work/apartment_split_v2/ (see decompose_rooms.py) -- the same scripted
forward/turn/forward tour drive_apartment.py drives interactively, but run
for 3 full loops with no GUI so it's fast to iterate on THRESHOLD_M without
babysitting a viewer window. This is exactly the check used to confirm the
0.1m/686-hull and 0.05m/1283-hull decompositions + implicitfast integrator
combo were stable (see sim-mujoco/README.md's "Tightening the hulls
further" section) -- run it again after any decompose_rooms.py
regeneration before trusting the result.

Flags any step where the robot's speed exceeds a sane bound (car crash
territory, not "diverged to NaN yet" -- catches the corner-catch impulse
spikes before they fully explode) or where qpos goes non-finite.

Usage: uv run stress_test_apartment.py [loops]
    (default 3 loops of the ~35s tour)
"""

import sys
from pathlib import Path

import mujoco
import numpy as np

import drive_apartment as da
from common import SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG, apply_drive_force, set_spawn

SPEED_ALARM = 20.0  # m/s -- comfortably above the ~3 m/s a clean drive ever reaches


def main() -> None:
    loops = int(sys.argv[1]) if len(sys.argv) > 1 else 3

    split_dir = Path(__file__).resolve().parent / "work" / "apartment_split_v2"
    rooms = da.find_decomposed_rooms(split_dir)
    total_hulls = sum(len(pieces) for pieces in rooms.values())
    if not rooms:
        raise SystemExit(f"no <room>/<room>_collision_*.obj found under {split_dir} -- run decompose_rooms.py first")
    print(f"{total_hulls} hulls across {len(rooms)} rooms")

    model = mujoco.MjModel.from_xml_string(da.build_world_xml(rooms))
    print(f"integrator={model.opt.integrator} (3 = implicitfast)")
    data = mujoco.MjData(model)
    set_spawn(model, data, SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG)
    mujoco.mj_forward(model, data)

    tour_length = da.TOUR[-1][0]
    total_steps = int(loops * tour_length / model.opt.timestep)
    max_speed = 0.0

    for step in range(total_steps):
        t = data.time % tour_length
        vx = wz = 0.0
        for t_end, phase_vx, phase_wz in da.TOUR:
            if t < t_end:
                vx, wz = phase_vx, phase_wz
                break

        apply_drive_force(model, data, vx, wz)
        mujoco.mj_step(model, data)

        if not np.all(np.isfinite(data.qpos)):
            print(f"DIVERGED (non-finite qpos) at step {step}, t={data.time:.2f}s")
            return

        speed = float(np.linalg.norm(data.qvel[0:3]))
        max_speed = max(max_speed, speed)
        if speed > SPEED_ALARM:
            print(f"SPEED SPIKE at step {step}, t={data.time:.2f}s: {speed:.1f} m/s, pos={data.qpos[:3]}")
            return

    print(f"OK: {loops} loop(s) ({tour_length * loops:.0f}s) with no divergence, max speed {max_speed:.2f} m/s")


if __name__ == "__main__":
    main()
