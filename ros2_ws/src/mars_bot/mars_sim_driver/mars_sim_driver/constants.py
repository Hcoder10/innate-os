# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Camera wire constants shared by the ROS adapter (node.py) and the world
side (core.py / world_server.py). Kept MuJoCo-free on purpose: the adapter
runs in the OS container, which does not ship MuJoCo -- the world itself
always runs on the host (see world_server.py)."""

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
