#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Head-camera <-> floor (z=0) via pinhole + URDF. Keep in sync with pickOverlay.js."""

import math

HEAD_ORIGIN = (-0.040751, -0.0002, 0.25882)  # base_link -> head joint (URDF)
CAM_IN_HEAD = (0.04327, 0.0297, -0.000275)   # head -> left camera optical
IMG_W, IMG_H = 640, 480
HFOV_DEG = 70.0


def _head_rot(tilt_rad):
    c, s = math.cos(tilt_rad), math.sin(tilt_rad)
    return ((c, 0.0, -s), (0.0, 1.0, 0.0), (s, 0.0, c))


def _rot(R, v):
    return tuple(sum(R[i][k] * v[k] for k in range(3)) for i in range(3))


def pixel_to_floor(u, v, head_tilt_deg):
    """Pixel (u,v) -> floor (x,y) in base_link, or None."""
    fx = IMG_W / (2.0 * math.tan(math.radians(HFOV_DEG) / 2.0))
    cx, cy = IMG_W / 2.0, IMG_H / 2.0
    R = _head_rot(math.radians(head_tilt_deg))
    cam = tuple(HEAD_ORIGIN[i] + _rot(R, CAM_IN_HEAD)[i] for i in range(3))
    fwd, right, down = _rot(R, (1, 0, 0)), _rot(R, (0, -1, 0)), _rot(R, (0, 0, -1))
    xo, yo = (u - cx) / fx, (v - cy) / fx
    d = tuple(fwd[i] + xo * right[i] + yo * down[i] for i in range(3))
    if d[2] >= -1e-6:
        return None
    t = -cam[2] / d[2]
    x, y = cam[0] + t * d[0], cam[1] + t * d[1]
    return (x, y) if x > 0 else None


def floor_to_pixel(x, y, head_tilt_deg):
    """Floor (x,y) -> pixel, or None. Inverse of pixel_to_floor."""
    fx = IMG_W / (2.0 * math.tan(math.radians(HFOV_DEG) / 2.0))
    cx, cy = IMG_W / 2.0, IMG_H / 2.0
    R = _head_rot(math.radians(head_tilt_deg))
    cam = tuple(HEAD_ORIGIN[i] + _rot(R, CAM_IN_HEAD)[i] for i in range(3))
    fwd, right, down = _rot(R, (1, 0, 0)), _rot(R, (0, -1, 0)), _rot(R, (0, 0, -1))
    D = (x - cam[0], y - cam[1], -cam[2])
    a = sum(D[i] * fwd[i] for i in range(3))
    if a <= 1e-6:
        return None
    b = sum(D[i] * right[i] for i in range(3))
    c = sum(D[i] * down[i] for i in range(3))
    return (cx + (b / a) * fx, cy + (c / a) * fx)
