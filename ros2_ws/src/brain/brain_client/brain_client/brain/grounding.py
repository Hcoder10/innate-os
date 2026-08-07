# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Visual navigation grounding: a pointed-at pixel -> a floor target.

The model points at a spot in the camera frame (normalized 0-1000 image
coordinates, the convention Gemini uses natively); this module casts that
pixel's ray through the calibrated camera geometry and intersects it with the
ground plane, yielding a robot-frame (x forward, y left) navigation target.

The pixel<->angle mapping is the same linear approximation the cloud brain's
``projection_utils`` used for its candidate-point markers (angle proportional
to pixel offset), with the identical camera model: mounted ``cam_forward``
ahead of base_link at ``cam_height``, pitched about Y by the head angle
(degrees, negative = looking down — the /mars/head convention).

PURE module: math only, no ROS. The actual frame aspect ratio is read from the
JPEG header rather than trusted from config, because the camera driver's
output size can differ from the calibrated nominal.
"""

from __future__ import annotations

import math

# Beyond this the ray grazes the floor and centimeter-level pixel error turns
# into meters of range error; the target is capped and approached in steps.
MAX_RANGE_M = 3.5

# Stop short of the pointed spot so the robot ends up facing it, not on it —
# close enough that the spot lands in the gripper's working range, which is the
# whole point of driving there.
#
# How short depends on what is AT the spot. The goal is a base_link pose, but
# what arrives first is whatever sticks out ahead of base_link — with the arm
# out that is the gripper, not the bumper, and a constant standoff drove the
# arm into a wall (R7-44, 2026-08-06). So: aim TARGET_STANDOFF_M short (the
# original tuning), and let the lidar clamp the travel wherever a return sits
# in the approach corridor — the margin exists only where a wall does.
TARGET_STANDOFF_M = 0.35  # spot-to-base_link distance to aim for on clear floor
GOAL_XY_TOL_M = 0.15  # goal_checker_precise xy_tolerances[0], mars_nav/config/controller.yaml
# Held objects are in no URDF and the costmap grid is 5cm. NOTE: a tape check
# (R7-44, 2026-08-07) found the lidar reading a wall ~10cm LONG — this margin
# absorbs part of that; widen it before trusting the guard on tighter work.
CLEARANCE_M = 0.10
FALLBACK_FRONT_M = 0.49  # padded hull with the arm fully extended: the safe assumption
DEFAULT_HALF_WIDTH_M = 0.165  # static footprint half-width, mars_nav/config/costmap.yaml
CORRIDOR_PAD_M = 0.05  # lateral margin beyond the half-width when sweeping the corridor
SELF_HIT_PAD_M = 0.05  # returns inside front+this are the arm crossing the scan plane


def jpeg_dimensions(jpeg: bytes) -> tuple[int, int] | None:
    """Read (width, height) from a JPEG's SOF marker without decoding pixels."""
    if len(jpeg) < 2 or jpeg[0] != 0xFF or jpeg[1] != 0xD8:
        return None
    i, n = 2, len(jpeg)
    while i < n:
        if jpeg[i] != 0xFF:
            i += 1
            continue
        while i < n and jpeg[i] == 0xFF:  # skip fill bytes before the marker
            i += 1
        if i >= n:
            break
        marker = jpeg[i]
        i += 1
        # Standalone markers (SOI/EOI/TEM/RSTn) carry no length field.
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 1 >= n:
            break
        # SOF markers (0xC0-0xCF, except DHT/JPG/DAC) hold the frame size.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 6 >= n:
                break
            height = (jpeg[i + 3] << 8) | jpeg[i + 4]
            width = (jpeg[i + 5] << 8) | jpeg[i + 6]
            return (width, height)
        i += (jpeg[i] << 8) | jpeg[i + 1]  # skip this marker segment
    return None


def pixel_to_floor(
    u_norm: float,
    v_norm: float,
    *,
    frame_jpeg: bytes,
    vertical_fov_deg: float,
    pitch_deg: float,
    cam_height: float,
    cam_forward: float,
) -> tuple[float, float] | None:
    """Project a pointed-at pixel onto the floor.

    Args:
        u_norm, v_norm: normalized image coordinates, 0-1000 from left / from top.
        frame_jpeg: the exact frame the coordinates refer to (for its aspect ratio).
        vertical_fov_deg: camera vertical field of view.
        pitch_deg: head pitch at capture time (degrees, negative = down).
        cam_height / cam_forward: camera mount relative to base_link (meters).

    Returns:
        (x forward, y left) in the robot frame at capture time, range-capped to
        :data:`MAX_RANGE_M`; None when the pixel is at or above the horizon.
    """
    dims = jpeg_dimensions(frame_jpeg)
    aspect = (dims[0] / dims[1]) if dims else 4.0 / 3.0
    v_fov = math.radians(vertical_fov_deg)
    h_fov = 2.0 * math.atan(math.tan(v_fov / 2.0) * aspect)

    # Pixel offset from image center -> view angles (positive = left / up).
    h_ang = (500.0 - u_norm) / 1000.0 * h_fov
    v_ang = (500.0 - v_norm) / 1000.0 * v_fov

    # Ray in the camera frame (x forward, y left, z up), then pitch about Y
    # into the robot frame. Same rotation as the cloud's ground-plane model:
    # theta = -pitch, so a downward-looking head tilts the ray toward the floor.
    dx_cam, dy, dz_cam = 1.0, math.tan(h_ang), math.tan(v_ang)
    theta = math.radians(-pitch_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    dx = cos_t * dx_cam + sin_t * dz_cam
    dz = -sin_t * dx_cam + cos_t * dz_cam

    if dz >= -1e-6:
        return None  # at or above the horizon: the ray never reaches the floor

    scale = cam_height / -dz
    x = cam_forward + scale * dx
    y = scale * dy

    distance = math.hypot(x, y)
    if distance > MAX_RANGE_M:
        x *= MAX_RANGE_M / distance
        y *= MAX_RANGE_M / distance
    return (x, y)


def standoff(front_extent: float | None) -> float:
    """How far short of the pointed spot base_link must stop when it can't see.

    The blind-conservative standoff, used when no scan is available to sweep
    the corridor: assume a wall at the spot. ``front_extent`` is the robot's
    forward reach from base_link (the max x of the live /footprint hull, arm
    included); None falls back to the worst-case reach, so a missing footprint
    feed degrades to stopping early, never to driving into the spot.
    """
    if front_extent is None:
        front_extent = FALLBACK_FRONT_M
    return front_extent + GOAL_XY_TOL_M + CLEARANCE_M


def clear_travel(
    floor_x: float,
    floor_y: float,
    obstacles: list[tuple[float, float]],
    *,
    front: float,
    half_width: float,
) -> float:
    """Max travel toward the spot that keeps the front clear of every return.

    ``obstacles`` are lidar returns in base_link (x forward, y left). A return
    counts when it lies ahead in a corridor ``half_width + CORRIDOR_PAD_M``
    wide around the drive line; the travel budget then keeps ``front`` plus
    the nav goal tolerance plus clearance short of it. Returns inside
    ``front + SELF_HIT_PAD_M`` are the robot's own arm crossing the scan
    plane, not the world. Corridor clear -> +inf (no clamp).
    """
    distance = math.hypot(floor_x, floor_y)
    if distance < 1e-6:
        return 0.0
    ux, uy = floor_x / distance, floor_y / distance
    guard = math.inf
    for px, py in obstacles:
        if math.hypot(px, py) <= front + SELF_HIT_PAD_M:
            continue  # the arm in the scan plane
        along = px * ux + py * uy
        if along <= 0.0:
            continue  # behind the drive line
        lateral = abs(py * ux - px * uy)
        if lateral > half_width + CORRIDOR_PAD_M:
            continue  # passes beside it
        guard = min(guard, along - front - GOAL_XY_TOL_M - CLEARANCE_M)
    return guard


def approach_goal(
    floor_x: float,
    floor_y: float,
    front_extent: float | None = None,
    *,
    obstacles: list[tuple[float, float]] | None = None,
    half_width: float | None = None,
) -> dict:
    """navigate_to_position inputs that approach the spot, facing it.

    With a scan (``obstacles``), aim :data:`TARGET_STANDOFF_M` short — close
    enough to work on the spot — clamped by :func:`clear_travel` so anything
    the lidar sees in the corridor keeps its distance. Without one, fall back
    to the blind :func:`standoff`.
    """
    distance = math.hypot(floor_x, floor_y)
    front = FALLBACK_FRONT_M if front_extent is None else front_extent
    if obstacles is None:
        travel = distance - standoff(front_extent)
    else:
        width = DEFAULT_HALF_WIDTH_M if half_width is None else half_width
        guard = clear_travel(floor_x, floor_y, obstacles, front=front, half_width=width)
        travel = min(distance - TARGET_STANDOFF_M, guard)
    travel = max(travel, 0.0)
    ratio = travel / distance if distance > 1e-6 else 0.0
    heading = math.atan2(floor_y, floor_x)
    return {
        "x": floor_x * ratio,
        "y": floor_y * ratio,
        "theta_degrees": math.degrees(heading),
        "local_frame": True,
    }
