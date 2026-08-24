#!/usr/bin/env python3
"""Prove local_to_absolute_nav_command is the exact inverse of its twin.

A sign error here does not crash. It sends the robot to a point mirrored about
its own heading -- plausible, on the map, and wrong -- and the only symptom is
a benchmark score that looks like an agent with poor spatial reasoning. Since
the stack cannot be run right now, the transform has to be checked against
something that is already trusted, and the trusted thing is
`absolute_to_local_nav_command`, which the shipped mapfree path has been using
all along.

So: for random robot poses and random goals, converting a map goal to local and
back must return the original. That pins rotation direction, translation order,
and angle wrapping at once, without a robot.

  usage: test_map_frame.py     (exit 0 = all pass)
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "ros2_ws/src/brain/brain_client/brain_client"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "ros2_ws/src/brain/brain_client"))

from brain_client.perception.pose import (  # noqa: E402
    absolute_to_local_nav_command,
    compute_pose_delta,
    local_to_absolute_nav_command,
)

FAILURES: list[str] = []
TOL = 1e-9


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name}  {detail}")


def angles_equal(a: float, b: float) -> bool:
    return abs(math.atan2(math.sin(a - b), math.cos(a - b))) < 1e-9


def main() -> int:
    print("map-frame goal conversion:")
    rng = random.Random(20260814)

    worst = 0.0
    for _ in range(2000):
        robot = (rng.uniform(-5, 5), rng.uniform(-5, 5), rng.uniform(-math.pi, math.pi))
        goal = {"x": rng.uniform(-5, 5), "y": rng.uniform(-5, 5),
                "theta_degrees": rng.uniform(-180, 180)}
        local = absolute_to_local_nav_command(dict(goal), robot)
        back = local_to_absolute_nav_command(local, robot)
        worst = max(worst, abs(back["x"] - goal["x"]), abs(back["y"] - goal["y"]))
        if not angles_equal(math.radians(back["theta_degrees"]), math.radians(goal["theta_degrees"])):
            worst = 1.0
    check("round trip over 2000 random poses and goals", worst < 1e-9,
          f"worst error {worst:.2e} m")

    # The convention itself, stated as a case a human can check by eye: robot
    # at the origin facing +y (90 deg), goal 1m "forward" and 0m lateral must
    # land at (0, 1) -- not (1, 0), which is what a missing rotation gives.
    facing_north = (0.0, 0.0, math.pi / 2)
    out = local_to_absolute_nav_command(
        {"x": 1.0, "y": 0.0, "theta_degrees": 0.0, "local_frame": True}, facing_north)
    check("1m ahead of a north-facing robot is (0, 1)",
          abs(out["x"]) < TOL and abs(out["y"] - 1.0) < TOL,
          f"got ({out['x']:.3f}, {out['y']:.3f})")

    # Positive lateral is LEFT (compute_pose_delta's docstring). Facing north,
    # left is -x. A sign flip here mirrors every grounded goal.
    out = local_to_absolute_nav_command(
        {"x": 0.0, "y": 1.0, "theta_degrees": 0.0, "local_frame": True}, facing_north)
    check("1m to the left of a north-facing robot is (-1, 0)",
          abs(out["x"] + 1.0) < TOL and abs(out["y"]) < TOL,
          f"got ({out['x']:.3f}, {out['y']:.3f})")

    # Agreement with the delta helper the rest of the brain uses.
    robot = (1.5, -2.0, 0.7)
    target = (3.0, 0.5, -1.2)
    fwd, lat, dth = compute_pose_delta(robot, target)
    out = local_to_absolute_nav_command(
        {"x": fwd, "y": lat, "theta": dth, "local_frame": True}, robot)
    check("agrees with compute_pose_delta",
          abs(out["x"] - target[0]) < 1e-9 and abs(out["y"] - target[1]) < 1e-9
          and angles_equal(out["theta"], target[2]),
          f"got ({out['x']:.3f}, {out['y']:.3f}, {out['theta']:.3f})")

    # An absolute goal must pass through untouched, or a second conversion
    # would translate an already-converted goal a second time.
    absolute = {"x": 2.0, "y": 3.0, "theta_degrees": 45.0}
    check("absolute goals pass through unchanged",
          local_to_absolute_nav_command(dict(absolute), robot) == absolute)

    # The theta spelling must survive: writing back the other key would leave
    # two conflicting headings in one dict.
    out = local_to_absolute_nav_command(
        {"x": 1.0, "y": 0.0, "theta": 0.3, "local_frame": True}, robot)
    check("radians in, radians out", "theta" in out and "theta_degrees" not in out)
    out = local_to_absolute_nav_command(
        {"x": 1.0, "y": 0.0, "theta_degrees": 30.0, "local_frame": True}, robot)
    check("degrees in, degrees out", "theta_degrees" in out and "theta" not in out)
    check("local_frame is cleared", out.get("local_frame") is False)

    print(f"\n{'FAILED' if FAILURES else 'all pass'}"
          f" ({len(FAILURES)} failure{'s' if len(FAILURES) != 1 else ''})")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
