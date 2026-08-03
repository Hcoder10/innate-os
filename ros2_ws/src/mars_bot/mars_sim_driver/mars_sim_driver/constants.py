# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Camera wire constants shared by the ROS adapter (node.py) and the world
side (core.py / world_server.py). Kept MuJoCo-free on purpose: the adapter
runs in the OS container, which does not ship MuJoCo -- the world itself
always runs on the host (see world_server.py)."""

import math

CAMERA_WIDTH, CAMERA_HEIGHT = 640, 480
# Vertical FOV matching the REAL head camera's focal length, best-estimated
# at fx ~= 355 @640x480 (back-solved from pick_any_object's hardware tuning
# note "0.37 grasps at ~0.32"; consistent with stereo_depth_estimator.yaml's
# blind-range comment). The previous 80 degrees was the viewer's display
# FOV, not the camera -- through it the skill's range model over-read 1.6x
# instead of the 1.3x its tuning cancels. Replace with camera_info's K[4]
# when read off a calibrated robot; sim/viewer's ROBOT_CAMERA_VFOV tracks
# this.
CAMERA_FOVY = 68.5  # 2*atan(240/355), in degrees

# The wrist camera is a different, uncalibrated lens; the skill's wrist-servo
# constants are tuned on it and converge in sim at 80, so it keeps 80 until
# someone measures the real module.
WRIST_CAMERA_FOVY = 80

# The nav policy only ever saw the mars preset's front_rect view: a 640x480
# pinhole at 110 deg HORIZONTAL fov. main is 84.5 deg horizontal -- a 1.57x
# longer focal length, which the policy reads as floor distances 36% nearer.
NAV_CAMERA_HFOV = 110.0
NAV_CAMERA_FOVY = math.degrees(
    2 * math.atan(math.tan(math.radians(NAV_CAMERA_HFOV / 2)) * CAMERA_HEIGHT / CAMERA_WIDTH)
)
