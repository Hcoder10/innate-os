"""Headless check of virtual_mars_core (no ROS needed): settle, render both
cameras, drive via cmd_vel, verify motion and the stale-command watchdog.
Saves the rendered frames to work/virtual_mars/ for eyeballing.

Usage: uv run test_virtual_mars.py
"""

import math
from pathlib import Path

from virtual_mars_core import VirtualMars

OUT_DIR = Path(__file__).resolve().parent / "work" / "virtual_mars"


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
