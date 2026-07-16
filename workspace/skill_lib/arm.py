#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Arm primitives shared by skills. The hardware lessons live here once.

Plain functions: pass the manipulation interface (and, where needed, a
joint-state getter) explicitly — no ambient context, no Skill machinery.
Rule of thumb: short blocking device commands, recovery sequences, and
pure math belong here; anything long-running or cancellable stays a Skill.

Import gotcha (applies to all of workspace/skill_lib): import at the TOP of
a skill file. The skill loader puts the repo root on sys.path only while the
skill module executes, so a lazy import inside execute() will not resolve.
"""

import math
import time

# The arm's grasp-reach box in base_link meters — beyond it, IK strains or
# the fingers land short. The webapp draws this box (REACH in pickTunePanel.js).
REACH_X = (0.22, 0.40)
REACH_Y = (-0.10, 0.10)


class ArmUnhealthy(RuntimeError):
    """Servo brownout / refusal — abort loudly, never continue limp."""


def clamp_reach(x, y):
    """Clamp a grasp / servo target into the arm's reach box."""
    return (max(REACH_X[0], min(REACH_X[1], x)),
            max(REACH_Y[0], min(REACH_Y[1], y)))


def ee_xyz(manipulation):
    """Current end-effector position from FK, or None while state is absent."""
    pose = manipulation.get_current_end_effector_pose()
    try:
        p = pose["position"]
        return (float(p["x"]), float(p["y"]), float(p["z"]))
    except (KeyError, TypeError):
        return None


def gripper_j6(joint_states):
    """Gripper joint (j6) from a joint_states dict, or None."""
    try:
        return joint_states["position"][5] if joint_states else None
    except (KeyError, IndexError, TypeError):
        return None


def recover(manipulation, logger=None):
    """Reboot servos + torque on — the only cure for an overcurrent trip or
    a brownout (torque_on alone can't clear a tripped servo's hardware error)."""
    if logger:
        logger.warning("[arm] recovering (reboot + torque on)")
    manipulation.reboot_servos()
    time.sleep(2.0)
    manipulation.torque_on()
    time.sleep(0.5)


def move_checked(manipulation, x, y, z, pitch, duration=1.5, tol=0.05, logger=None):
    """Cartesian move with a health check: verify FK landed within tol,
    recover + retry once, then raise ArmUnhealthy rather than let a limp
    arm continue into the floor."""
    for attempt in (1, 2):
        ok = manipulation.move_to_cartesian_pose(
            x=x, y=y, z=z, roll=0.0, pitch=pitch, yaw=0.0,
            duration=duration, blocking=True,
        )
        cur = ee_xyz(manipulation)
        err = math.dist(cur, (x, y, z)) if cur is not None else None
        if ok and err is not None and err <= tol:
            return True
        if logger:
            logger.warning(f"[arm] not tracking (ok={ok} err={err}) — "
                           f"{'recovering' if attempt == 1 else 'giving up'}")
        if attempt == 1:
            recover(manipulation, logger)
    raise ArmUnhealthy(f"arm failed to reach ({x:.2f},{y:.2f},{z:.2f})")


def open_checked(manipulation, get_j6, percent=100.0, duration=1.0,
                 logger=None, on_reboot=None):
    """Open the gripper and VERIFY it opened. A prior hard close can
    overcurrent-TRIP the gripper servo — a hardware error torque_on alone
    can't clear — after which open silently does nothing and the hand stays
    shut. If j6 says it didn't open, reboot to clear the trip and retry.
    on_reboot(j6) is called before the reboot (telemetry hook)."""
    manipulation.torque_on()  # a torque-disabled servo won't move at all
    ok = manipulation.open_gripper(percent=percent, duration=duration, blocking=True)
    j6 = get_j6()
    if j6 is not None and j6 < 0.10:
        if logger:
            logger.warning(f"[arm] gripper did not open (j6={j6:.3f}); "
                           "rebooting servos to clear a trip, then retrying")
        if on_reboot:
            on_reboot(j6)
        recover(manipulation, logger)
        ok = manipulation.open_gripper(percent=percent, duration=duration, blocking=True)
    return bool(ok)


def close(manipulation, strength=0.0, duration=1.0):
    """Close the gripper. strength = extra squeeze past the closed stop.
    Keep it <= ~0.6 against a real object: higher stalls j6 at max current
    on the fingers and overcurrent-trips the servo (open then no-ops until
    a reboot)."""
    manipulation.torque_on()
    return bool(manipulation.close_gripper(strength=strength, duration=duration,
                                           blocking=True))
