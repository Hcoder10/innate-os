#!/usr/bin/env python3
"""
CameraProvider – lightweight ROS 2 node that subscribes to camera topics
in its own spin thread, storing raw compressed bytes.

Runs independently of the main executor so camera callbacks are never
starved by long-running action-server work.  Base64 encoding is deferred
to property access so the callback stays as fast as possible.

Subscriptions are created on-demand via start()/stop() so the node
consumes zero CPU when no skill needs camera data.
"""

import base64
import threading

import rclpy
import rclpy.executors
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage


class CameraProvider(Node):
    """Subscribe to camera topics in a dedicated background thread.

    Raw compressed bytes are stored on every callback (cheap memcpy).
    Base64 strings are computed lazily via properties so the cost is
    only paid when a consumer actually reads the value.

    Call start() before reading camera data and stop() when done.
    """

    _IMAGE_QOS = QoSProfile(
        reliability=QoSReliabilityPolicy.BEST_EFFORT,
        history=QoSHistoryPolicy.KEEP_LAST,
        depth=1,
    )

    # Each skill that needs the camera builds its own provider, so the node name
    # must be unique — otherwise rcl warns "Publisher already registered for
    # provided node name" for every collision.
    _instance_count = 0

    def __init__(self):
        CameraProvider._instance_count += 1
        super().__init__(f"camera_subscriber_{CameraProvider._instance_count}")

        self._main_camera_raw: bytes | None = None
        self._wrist_camera_raw: bytes | None = None

        self._main_sub = None
        self._wrist_sub = None
        self._executor: rclpy.executors.SingleThreadedExecutor | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    # ---- lifecycle ----

    def start(self):
        """Create subscriptions and begin spinning in a background thread."""
        if self._running:
            return
        self._main_sub = self.create_subscription(
            CompressedImage,
            "/mars/main_camera/left/image_raw/compressed",
            self._main_camera_cb,
            self._IMAGE_QOS,
        )
        self._wrist_sub = self.create_subscription(
            CompressedImage,
            "/mars/arm/image_raw/compressed",
            self._wrist_camera_cb,
            self._IMAGE_QOS,
        )
        self._executor = rclpy.executors.SingleThreadedExecutor()
        self._executor.add_node(self)
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        self._running = True
        self.get_logger().info("Camera subscriptions started")

    def stop(self):
        """Destroy subscriptions and stop the background thread."""
        if not self._running:
            return
        if self._executor is not None:
            self._executor.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._executor = None
        for sub in (self._main_sub, self._wrist_sub):
            if sub is not None:
                self.destroy_subscription(sub)
        self._main_sub = None
        self._wrist_sub = None
        self._main_camera_raw = None
        self._wrist_camera_raw = None
        self._running = False
        self.get_logger().info("Camera subscriptions stopped")

    # ---- callbacks (as cheap as possible) ----

    def _spin(self):
        try:
            self._executor.spin()
        except Exception:
            pass

    def _main_camera_cb(self, msg: CompressedImage):
        self._main_camera_raw = bytes(msg.data)

    def _wrist_camera_cb(self, msg: CompressedImage):
        self._wrist_camera_raw = bytes(msg.data)

    # ---- lazy base64 properties ----

    @property
    def last_main_camera_b64(self) -> str | None:
        """Return the latest main camera frame as a base64 string, or None."""
        raw = self._main_camera_raw
        if raw is None:
            return None
        return base64.b64encode(raw).decode("utf-8")

    @property
    def last_wrist_camera_b64(self) -> str | None:
        """Return the latest wrist camera frame as a base64 string, or None."""
        raw = self._wrist_camera_raw
        if raw is None:
            return None
        return base64.b64encode(raw).decode("utf-8")

    # ---- cleanup ----

    def shutdown(self):
        self.stop()
