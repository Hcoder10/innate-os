"""Camera capture: RGB / arm / depth frames, the frame buffer, and head pitch.

Owns the on-demand sensor subscriptions created while the brain is active. Decodes
incoming frames and exposes them as ready-to-send blocks via the pure
:mod:`perception.image_codec` and :mod:`navigation.payload` helpers.
"""

from __future__ import annotations

import json
from collections import deque

import cv2
import numpy as np
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage, Image
from std_msgs.msg import String

from brain_client.navigation import payload
from brain_client.perception import image_codec

# depth encoding string -> numpy dtype (incoming sensor_msgs/Image)
_DEPTH_DTYPES = {"16UC1": np.uint16, "mono16": np.uint16, "32FC1": np.float32, "mono32": np.float32}


class CameraCapture:
    def __init__(self, node, config):
        self._node = node
        self._logger = node.get_logger()
        self._config = config

        self.last_image = None
        self.last_depth_image = None
        self.last_arm_camera = None
        self.current_head_pitch = 0.0
        self.image_buffer = deque(maxlen=config.image_buffer_max_size)

        self._image_sub = None
        self._depth_sub = None
        self._arm_sub = None
        self._head_sub = None

    def start(self) -> None:
        if self._image_sub is not None:
            return
        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=2,
        )
        cfg = self._config
        self._image_sub = self._node.create_subscription(CompressedImage, cfg.image_topic, self._on_image, image_qos)
        if cfg.send_depth:
            self._depth_sub = self._node.create_subscription(Image, cfg.depth_image_topic, self._on_depth, 1)
        if cfg.send_arm_camera_image:
            self._arm_sub = self._node.create_subscription(
                CompressedImage, cfg.arm_camera_image_topic, self._on_arm, image_qos
            )
        self._head_sub = self._node.create_subscription(String, "/mars/head/current_position", self._on_head, 10)

    def stop(self) -> None:
        for sub in (self._image_sub, self._depth_sub, self._arm_sub, self._head_sub):
            if sub is not None:
                self._node.destroy_subscription(sub)
        self._image_sub = self._depth_sub = self._arm_sub = self._head_sub = None
        self.last_image = None
        self.last_depth_image = None
        self.last_arm_camera = None
        self.image_buffer.clear()

    # --- callbacks ---
    def _on_image(self, msg: CompressedImage) -> None:
        try:
            decoded = cv2.imdecode(np.frombuffer(msg.data, np.uint8), cv2.IMREAD_COLOR)
            if decoded is not None:
                self.last_image = decoded
                t = self._node.get_clock().now().nanoseconds / 1e9
                self.image_buffer.append((t, decoded))
            else:
                self._logger.warn("Failed to decode image (decoded is None).")
        except Exception as e:
            self._logger.error(f"Failed to decode compressed image: {e}")

    def _on_arm(self, msg: CompressedImage) -> None:
        try:
            self.last_arm_camera = cv2.imdecode(np.frombuffer(msg.data, np.uint8), cv2.IMREAD_COLOR)
        except Exception as e:
            self._logger.error(f"Failed to decode arm camera image: {e}")

    def _on_depth(self, msg: Image) -> None:
        try:
            dtype = _DEPTH_DTYPES.get(msg.encoding)
            if dtype is None:
                self._logger.warn(f"Unexpected depth encoding: {msg.encoding}, defaulting to uint8")
                dtype = np.uint8
            self.last_depth_image = np.frombuffer(msg.data, dtype=dtype).reshape((msg.height, msg.width))
        except Exception as e:
            self._logger.error(f"Failed to decode depth image: {e}")

    def _on_head(self, msg: String) -> None:
        try:
            self.current_head_pitch = json.loads(msg.data).get("current_position", 0.0)
        except (json.JSONDecodeError, KeyError) as e:
            self._logger.error(f"Failed to parse head position: {e}")
            self.current_head_pitch = 0.0

    # --- queries / encoded blocks ---
    @property
    def has_image(self) -> bool:
        return self.last_image is not None

    def latest_image_b64(self) -> str | None:
        return image_codec.encode_jpeg_b64(self.last_image)

    def recent_frames(self, window_seconds: float) -> list:
        now = self._node.get_clock().now().nanoseconds / 1e9
        return [img for ts, img in list(self.image_buffer) if now - ts <= window_seconds]

    def depth_block(self) -> dict | None:
        if self._config.send_depth and self.last_depth_image is not None:
            return payload.encode_depth(self.last_depth_image)
        return None

    def arm_block(self) -> dict | None:
        if self._config.send_arm_camera_image and self.last_arm_camera is not None:
            arm_b64 = image_codec.encode_jpeg_b64(self.last_arm_camera)
            if arm_b64:
                return {"image_b64": arm_b64, "camera_type": "arm_wrist"}
        return None

    def clear_after_send(self) -> None:
        """Reset depth/arm so the same frames are not resent."""
        self.last_depth_image = None
        self.last_arm_camera = None
