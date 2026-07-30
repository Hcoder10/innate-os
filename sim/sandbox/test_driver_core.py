"""Headless check of mars_sim_driver.core (no ROS needed): settle, render
both cameras, drive via cmd_vel, verify motion and the stale-command
watchdog. Saves the rendered frames to sim/assets/virtual_mars_test/ for
eyeballing.

Usage: uv run sandbox/test_driver_core.py
"""

import math

import _driver_pkg  # noqa: F401
from mars_sim_driver import world
from mars_sim_driver.core import VirtualMars, joint2_min_target

OUT_DIR = world.default_assets_dir() / "virtual_mars_test"


def main() -> None:
    sim = VirtualMars()
    sim.step(1.0)  # settle from spawn drop

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for cam in ("main", "wrist"):
        jpeg = sim.render_jpeg(cam)
        assert jpeg[:2] == b"\xff\xd8", f"{cam}: not a JPEG"
        (OUT_DIR / f"{cam}.jpg").write_bytes(jpeg)
        print(f"{cam}: {len(jpeg)} byte JPEG -> {OUT_DIR / f'{cam}.jpg'}")

    x0, y0, yaw0 = sim.pose()
    for _ in range(20):  # re-send like a teleop publisher so the watchdog stays fed
        sim.set_cmd_vel(0.3, 0.0)
        sim.step(0.1)
    x1, y1, _ = sim.pose()
    moved = math.hypot(x1 - x0, y1 - y0)
    assert moved > 0.3, f"drove only {moved:.2f}m"
    print(f"cmd_vel drive: moved {moved:.2f}m")

    sim.step(1.0)  # no new commands -> watchdog stops the base
    x2, y2, _ = sim.pose()
    sim.step(0.5)
    x3, y3, _ = sim.pose()
    drift = math.hypot(x3 - x2, y3 - y2)
    assert drift < 0.05, f"base still moving {drift:.2f}m after cmd_vel went stale"
    print(f"watchdog: base stopped (drift {drift * 100:.1f}cm)")

    assert abs(sim.head_pitch_deg()) < 5.0
    print(f"head pitch: {sim.head_pitch_deg():.1f} deg")

    # Body guard: while joint1's target crosses the front arc, joint2 may not
    # stay folded past -0.5 (arm_control.cpp's floor) -- the arm ducks under
    # the head. Ramp math first, then the symptom that matters: a teleop-like
    # streamed joint1 sweep from home across the front must not jam.
    assert joint2_min_target(-1.4, -1.57) == -1.57  # outside the arc: full range
    assert joint2_min_target(0.0, -1.57) == -0.25  # front arc: duck
    assert abs(joint2_min_target(1.125, -1.57) - (-0.91)) < 1e-9  # mid-ramp
    assert joint2_min_target(1.25, -1.57) == -1.57
    home_j1 = world.ARM_HOME["joint1"]
    for leg_target in (-1.4, home_j1):  # across the front and back
        start = sim.joint_positions()["joint1"]
        for i in range(1, 41):  # ~0.07 rad per 100ms, teleop cadence
            sim.set_joint_target("joint1", start + (leg_target - start) * i / 40)
            sim.step(0.1)
        sim.step(3.0)
        p = sim.joint_positions()
        assert abs(p["joint1"] - leg_target) < 0.15, (
            f"joint1 stalled at {p['joint1']:.2f} sweeping to {leg_target:.2f}"
        )
    print(f"joint2 body guard: front-arc sweep ok both ways (j2 back at {p['joint2']:+.2f})")
    sim.reset()

    depth = sim.render_depth("main")
    center = float(depth[depth.shape[0] // 2, depth.shape[1] // 2])
    assert 0.1 < center < 15.0, f"implausible center depth {center}"
    print(f"depth: center pixel {center:.2f}m, range {depth.min():.2f}-{depth.max():.2f}m")

    scan = sim.lidar_scan(n_rays=360, max_range=12.0)
    hits = (scan < 12.0).sum()
    assert hits > 180, f"only {hits}/360 lidar rays hit indoors"
    assert scan.min() > 0.05, f"lidar sees something at {scan.min():.3f}m (inside the robot?)"
    print(f"lidar: {hits}/360 rays hit, nearest {scan.min():.2f}m, farthest hit {scan[scan < 12].max():.2f}m")
    print("PASS")


if __name__ == "__main__":
    main()
