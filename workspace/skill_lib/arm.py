#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared arm primitives (blocking moves, recovery, reach clamp).

Pass interfaces explicitly. Import at TOP of skill files — lazy imports
inside execute() fail (loader only puts repo root on path during load).
"""

import math
import time

# Grasp reach box (base_link m).
REACH_X = (0.22, 0.40)
REACH_Y = (-0.10, 0.10)

# joints 1-6 = base yaw, shoulder, elbow, wrist pitch, wrist roll, gripper.
# Folded rest shape. j4=0.30 keeps the gripper off the floor (ee_link z ~0.042 m);
# pitching it further down jams the fingers into the ground and trips a servo.
# These are the values the arm reaches and HOLDS — j1/j2 sit at their limits, so
# commanding "more folded" just settles here.
REST_POSITION = [1.5708, -1.2195, 1.5723, 0.30, 0.0, 0.0031]


class ArmUnhealthy(RuntimeError):
    """Servo brownout/refusal — abort, don't continue limp."""


def rest(manipulation, joint_states=None, duration=3, keep_gripper=True, cancelled=None):
    """Fold the arm to REST_POSITION (blocking up to `duration` seconds).

    keep_gripper preserves the current j6 closure so a held object isn't
    released. cancelled: optional predicate polled during the move; when it
    turns true the wait stops early. Returns False if the command was refused
    or the move was cancelled, else True.
    """
    target = list(REST_POSITION)
    if keep_gripper:
        j6 = gripper_j6(joint_states)
        if j6 is not None:
            target[5] = j6
    if cancelled is None:
        return bool(manipulation.move_to_joint_positions(joint_positions=target, duration=duration, blocking=True))
    if not manipulation.move_to_joint_positions(joint_positions=target, duration=duration, blocking=False):
        return False
    start = time.time()
    while time.time() - start < duration:
        if cancelled():
            return False
        time.sleep(0.1)
    return True


def clamp_reach(x, y):
    return (max(REACH_X[0], min(REACH_X[1], x)), max(REACH_Y[0], min(REACH_Y[1], y)))


def ee_xyz(manipulation):
    """FK end-effector (x,y,z), or None."""
    pose = manipulation.get_current_end_effector_pose()
    try:
        p = pose["position"]
        return (float(p["x"]), float(p["y"]), float(p["z"]))
    except (KeyError, TypeError):
        return None


def gripper_j6(joint_states):
    """Gripper joint j6, or None."""
    try:
        return joint_states["position"][5] if joint_states else None
    except (KeyError, IndexError, TypeError):
        return None


def recover(manipulation, logger=None):
    """Reboot servos + torque on (clears overcurrent trip / brownout)."""
    if logger:
        logger.warning("[arm] recovering (reboot + torque on)")
    manipulation.reboot_servos()
    time.sleep(2.0)
    manipulation.torque_on()
    time.sleep(0.5)


def move_checked(manipulation, x, y, z, pitch, duration=1.5, tol_xy=0.05, tol_z=0.10, logger=None):
    """Cartesian move; verify FK within per-axis tolerances, recover+retry once, else raise.

    tol_z is looser than tol_xy on purpose: a z shortfall usually means the
    fingers met the object/floor early (expected while descending), while xy
    error means the grasp is off target.
    """
    for attempt in (1, 2):
        ok = manipulation.move_to_cartesian_pose(
            x=x,
            y=y,
            z=z,
            roll=0.0,
            pitch=pitch,
            yaw=0.0,
            duration=duration,
            blocking=True,
        )
        cur = ee_xyz(manipulation)
        err_xy = math.hypot(cur[0] - x, cur[1] - y) if cur is not None else None
        err_z = abs(cur[2] - z) if cur is not None else None
        if ok and err_xy is not None and err_xy <= tol_xy and err_z <= tol_z:
            return True
        if logger:
            logger.warning(
                f"[arm] not tracking (ok={ok} err_xy={err_xy} err_z={err_z}) — "
                f"{'recovering' if attempt == 1 else 'giving up'}"
            )
        if attempt == 1:
            recover(manipulation, logger)
    raise ArmUnhealthy(f"arm failed to reach ({x:.2f},{y:.2f},{z:.2f})")


def open_checked(manipulation, get_j6, percent=100.0, duration=1.0, logger=None, on_reboot=None):
    """Open gripper and verify j6. Reboot+retry if tripped shut. on_reboot(j6) hook."""
    manipulation.torque_on()  # a torque-disabled servo won't move at all
    ok = manipulation.open_gripper(percent=percent, duration=duration, blocking=True)
    j6 = get_j6()
    if j6 is not None and j6 < 0.10:
        if logger:
            logger.warning(f"[arm] gripper did not open (j6={j6:.3f}); rebooting servos to clear a trip, then retrying")
        if on_reboot:
            on_reboot(j6)
        recover(manipulation, logger)
        ok = manipulation.open_gripper(percent=percent, duration=duration, blocking=True)
        # A tripped servo accepts the command and no-ops, so the retry's own
        # status proves nothing — j6 is the only evidence the claw moved.
        j6 = get_j6()
        if j6 is not None and j6 < 0.10:
            return False
    return bool(ok)


def close(manipulation, strength=0.0, duration=1.0):
    """Close gripper. strength = extra squeeze; ~0.8 holds well, watch for trips above."""
    manipulation.torque_on()
    return bool(manipulation.close_gripper(strength=strength, duration=duration, blocking=True))
