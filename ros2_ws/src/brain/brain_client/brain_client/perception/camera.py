# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Camera capture: the latest main and arm-wrist JPEG frames, plus head pitch.

Owns the on-demand sensor subscriptions created while the brain is active.
Frames arrive JPEG-compressed (sensor_msgs/CompressedImage) and are kept as raw
bytes with an arrival timestamp, so the brain can tell a live feed from a stale
one (a dead camera otherwise serves its last frame forever). The head pitch
(degrees, negative = looking down) is tracked because the pixel->floor
grounding needs the camera angle at frame-capture time.
"""

from __future__ import annotations

import json
import time

from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String


class CameraCapture:
    def __init__(self, node, config):
        self._node = node
        self._config = config
        self._image_sub = None
        self._arm_sub = None
        self._head_sub = None
        self._image: tuple[float, bytes] | None = None  # (monotonic arrival time, jpeg)
        self._arm: tuple[float, bytes] | None = None
        self.current_head_pitch = 0.0  # degrees; negative = looking down

    def start(self) -> None:
        if self._image_sub is not None:
            return
        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=2,
        )
        self._image_sub = self._node.create_subscription(
            CompressedImage, self._config.image_topic, self._on_image, image_qos
        )
        if self._config.send_arm_camera_image:
            self._arm_sub = self._node.create_subscription(
                CompressedImage, self._config.arm_camera_image_topic, self._on_arm, image_qos
            )
        self._head_sub = self._node.create_subscription(String, "/mars/head/current_position", self._on_head, 10)

    def stop(self) -> None:
        # Destroying here is safe only because brain_client_node is spun
        # single-threaded and stop() runs on that spin thread (between callbacks).
        for sub in (self._image_sub, self._arm_sub, self._head_sub):
            if sub is not None:
                self._node.destroy_subscription(sub)
        self._image_sub = self._arm_sub = self._head_sub = None
        self._image = self._arm = None

    def _on_image(self, msg: CompressedImage) -> None:
        if msg.data:
            self._image = (time.monotonic(), bytes(msg.data))

    def _on_arm(self, msg: CompressedImage) -> None:
        if msg.data:
            self._arm = (time.monotonic(), bytes(msg.data))

    def _on_head(self, msg: String) -> None:
        try:
            self.current_head_pitch = float(json.loads(msg.data).get("current_position", 0.0))
        except (json.JSONDecodeError, TypeError, ValueError):
            self.current_head_pitch = 0.0

    def fresh_image_jpeg(self, max_age_sec: float) -> bytes | None:
        return _fresh(self._image, max_age_sec)

    def fresh_arm_jpeg(self, max_age_sec: float) -> bytes | None:
        return _fresh(self._arm, max_age_sec)


def _fresh(frame: tuple[float, bytes] | None, max_age_sec: float) -> bytes | None:
    if frame is None or time.monotonic() - frame[0] > max_age_sec:
        return None
    return frame[1]
