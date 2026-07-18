#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared mobility primitives (odometry-closed rotate/drive, pixel P-servo).

Pass interfaces explicitly. Import at TOP of skill files — lazy imports
inside execute() fail (loader only puts repo root on path during load).
"""

import math
import time


def stop(mobility):
    mobility.send_cmd_vel(0.0, 0.0, 0.1)


def odom_xyt(odom):
    """(x, y, theta) from nested dict or flat dataclass, or None."""
    if odom is None:
        return None
    try:
        if isinstance(odom, dict):
            p = odom["pose"]["pose"]["position"]
            return (float(p["x"]), float(p["y"]), math.radians(float(odom["theta_degrees"])))
        return (float(odom.x), float(odom.y), float(odom.theta))
    except (KeyError, AttributeError, TypeError):
        return None


def servo_vel(err_px, gain, v_min, v_max, deadband_px):
    """Pixel P-servo axis: gain per 100 px, clamp to [v_min, v_max], 0 in deadband."""
    if abs(err_px) <= deadband_px:
        return 0.0
    v = max(-v_max, min(v_max, -gain / 100.0 * err_px))
    return math.copysign(v_min, v) if abs(v) < v_min else v


def rotate_by(
    mobility,
    get_xyt,
    angle,
    *,
    kp=1.2,
    wz_max=0.5,
    wz_min=0.15,
    tol=math.radians(2.5),
    timeout=12.0,
    cancelled=None,
    dbg=None,
):
    """Rotate in place by `angle` rad, closed on odometry yaw (open-loop if
    get_xyt yields None). cancelled: optional predicate polled each loop —
    when it turns true the base stops and the function returns early.
    dbg: optional callable(event, **fields) for telemetry.
    """
    if dbg:
        dbg("rotate", angle_deg=math.degrees(angle))
    xyt = get_xyt()
    if xyt is None:
        mobility.send_cmd_vel(0.0, math.copysign(0.35, angle), abs(angle) / 0.35)
        time.sleep(abs(angle) / 0.35 + 0.4)
        return
    target = xyt[2] + angle
    t0 = time.time()
    err = angle
    while time.time() - t0 < timeout:
        if cancelled is not None and cancelled():
            break
        xyt = get_xyt()
        if xyt is None:
            break
        err = math.atan2(math.sin(target - xyt[2]), math.cos(target - xyt[2]))
        if abs(err) < tol:
            break
        wz = max(-wz_max, min(wz_max, kp * err))
        if abs(wz) < wz_min:
            wz = math.copysign(wz_min, wz)
        mobility.send_cmd_vel(0.0, wz, 0.15)
        time.sleep(0.08)
    stop(mobility)
    if dbg:
        dbg("rotate_done", err_deg=math.degrees(err), s=time.time() - t0)


def drive(
    mobility,
    get_xyt,
    dist,
    *,
    kp=0.3,
    v_max=0.10,
    v_min=0.04,
    tol=0.015,
    timeout=15.0,
    cancelled=None,
    dbg=None,
    logger=None,
):
    """Drive straight by `dist` m, closed on odometry position (open-loop if
    get_xyt yields None). cancelled: optional predicate polled each loop —
    when it turns true the base stops and the function returns early.
    dbg: optional callable(event, **fields) for telemetry.
    """
    if abs(dist) < tol:
        return
    if dbg:
        dbg("drive", dist_m=dist)
    xyt = get_xyt()
    if xyt is None:
        if logger:
            logger.warning("[mobility] no odom — open-loop drive")
        mobility.send_cmd_vel(math.copysign(0.08, dist), 0.0, abs(dist) / 0.08)
        time.sleep(abs(dist) / 0.08 + 0.4)
        return
    x0, y0 = xyt[0], xyt[1]
    t0 = time.time()
    err = abs(dist)
    while time.time() - t0 < timeout:
        if cancelled is not None and cancelled():
            break
        xyt = get_xyt()
        if xyt is None:
            break
        gone = math.hypot(xyt[0] - x0, xyt[1] - y0)
        err = abs(dist) - gone
        if err < tol:
            break
        v = math.copysign(max(v_min, min(v_max, kp * err)), dist)
        mobility.send_cmd_vel(v, 0.0, 0.15)
        time.sleep(0.08)
    stop(mobility)
    if dbg:
        dbg("drive_done", err_m=err, s=time.time() - t0)
