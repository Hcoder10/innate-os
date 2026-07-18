#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared arm primitives (blocking moves, recovery, reach clamp).

Pass interfaces explicitly. Import at TOP of skill files — lazy imports
inside execute() fail (loader only puts repo root on path during load).

Poses are plain joint lists. ``go`` / ``rest`` / ``zero`` raise on failure
so call sites stay declarative:

    armlib.rest(self.manipulation, self.joint_states)
    armlib.go(self.manipulation, CARRY if holding else armlib.ZERO)
"""

import math
import time

# Grasp reach box (base_link m).
REACH_X = (0.22, 0.40)
REACH_Y = (-0.10, 0.10)

# joints 1-6 = base yaw, shoulder, elbow, wrist pitch, wrist roll, gripper.
# Folded rest with j4 lifted so the gripper clears the floor (verified live:
# ee_link z ~0.042 m). j1/j2 clamp to their limits, so this is what the arm
# actually reaches and holds.
REST = [1.5708, -1.2195, 1.5723, 0.30, 0.0, 0.0031]
ZERO = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

# Back-compat aliases.
REST_POSITION = REST
ZERO_POSITION = ZERO


class ArmUnhealthy(RuntimeError):
    """Servo brownout/refusal — abort, don't continue limp."""


class ArmFailed(RuntimeError):
    """Joint/cartesian command rejected or did not complete."""


class ArmCancelled(Exception):
    """Caller-supplied cancel check fired mid-wait."""


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


def with_gripper(joints, j6):
    """Copy of joints with j6 set (no-op if j6 is None)."""
    out = list(joints)
    if j6 is not None:
        out[5] = float(j6)
    return out


def rest_joints(joint_states=None, keep_gripper=True):
    """Joint target for the folded rest pose."""
    if keep_gripper:
        return with_gripper(REST, gripper_j6(joint_states))
    return list(REST)


def go(manipulation, joints, duration=3.0, *, times=1, pause=0.3,
       is_cancelled=None, logger=None):
    """Move to joint positions. Raises ArmFailed / ArmCancelled.

    ``times`` + ``pause`` repeat the move so the arm can settle (pick teardown).
    With ``is_cancelled``, the move is non-blocking and polled so a skill
    cancel can abort the wait; otherwise each move blocks for ``duration``.
    """
    joints = list(joints)
    blocking = is_cancelled is None
    for i in range(times):
        if logger:
            logger.info(
                f"[arm] joints {[round(j, 3) for j in joints]} over {duration}s"
            )
        ok = manipulation.move_to_joint_positions(
            joint_positions=joints, duration=duration, blocking=blocking,
        )
        if not ok:
            raise ArmFailed("Failed to send arm command")
        if is_cancelled is not None:
            t0 = time.time()
            while time.time() - t0 < duration:
                if is_cancelled():
                    raise ArmCancelled("Arm motion cancelled")
                time.sleep(0.1)
        if pause and i + 1 < times:
            time.sleep(pause)


# Back-compat name used by earlier call sites.
go_joints = go


def rest(manipulation, joint_states=None, duration=3.0, keep_gripper=True,
         is_cancelled=None, logger=None, **go_kw):
    """Fold to REST. keep_gripper preserves current j6 when held."""
    go(
        manipulation, rest_joints(joint_states, keep_gripper),
        duration=duration, is_cancelled=is_cancelled, logger=logger, **go_kw,
    )


def zero(manipulation, duration=3.0, is_cancelled=None, logger=None, **go_kw):
    """Move all joints to 0."""
    go(
        manipulation, ZERO, duration=duration,
        is_cancelled=is_cancelled, logger=logger, **go_kw,
    )


# Back-compat aliases.
rest_position = rest
zero_position = zero


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
        if ok and err_xy is not None and err_z is not None and err_xy <= tol_xy and err_z <= tol_z:
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
