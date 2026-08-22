# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Gaze System - Person tracking for MARS robot.

Features:
- InspireFace detection for autonomous person tracking
- Wheel-based panning (robot turns to face people)

Hardware: MARS robot
- Head tilt: -25° to +40° (single axis)
- Pan: Uses differential drive wheels to rotate body
"""

import json
import math
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

import cv2
import inspireface as isf
import numpy as np
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String

from brain_client.robot.head import Head
from brain_client.robot.mobility import Mobility

FACE_DEBUG_TOPIC = "/brain/face_debug"
FACE_DETECTION_MIN_CONFIDENCE = 0.3
FACE_CENTER_X_TOLERANCE = 0.18
FACE_CENTER_Y_TOLERANCE = 0.4  # skill lock box; not a stop for ambient tilt
FACE_CENTER_Y_DEADZONE = 0.08  # stop hunting once the face is near mid-frame
FACE_LOCK_SECONDS = 2.0
FACE_TRACK_HZ = 5.0
FACE_LOCK_FRAMES = max(1, round(FACE_LOCK_SECONDS * FACE_TRACK_HZ))
FACE_LOST_SECONDS = 3.0
FACE_SEARCH_START_TILT = 25
FACE_SEARCH_TILT_STEP = 3
TILT_KP = 0.55
MIN_TILT = -25
MAX_TILT = 40  # joint_7 look-up; +25 still left standing faces above the frame
CAMERA_HFOV = 100.0
CAMERA_VFOV = 80.0


def clamp_tilt(degrees: float) -> float:
    return max(MIN_TILT, min(MAX_TILT, degrees))


@dataclass(frozen=True)
class FaceBox:
    center_x: float
    center_y: float
    width: float
    height: float


class FaceDetector:
    """Face detector using InspireFace SDK."""

    def __init__(self, min_confidence: float = 0.5):
        param = isf.SessionCustomParameter()
        self._session = isf.InspireFaceSession(
            param=param,
            detect_mode=isf.HF_DETECT_MODE_ALWAYS_DETECT,
            max_detect_num=3,
        )
        self._session.set_detection_confidence_threshold(min_confidence)

    def detect(self, frame) -> list[FaceBox]:
        h, w = frame.shape[:2]
        faces = []
        for face in self._session.face_detection(frame):
            x1, y1, x2, y2 = face.location
            faces.append(
                FaceBox(
                    center_x=(x1 + x2) / 2 / w,
                    center_y=(y1 + y2) / 2 / h,
                    width=(x2 - x1) / w,
                    height=(y2 - y1) / h,
                )
            )
        return faces


class FaceDebugPublisher:
    """Publishes the tracking-state payload the webapp face overlay renders."""

    def __init__(self, node):
        self._publisher = node.create_publisher(String, FACE_DEBUG_TOPIC, 10)

    def publish(
        self,
        *,
        faces: list[FaceBox],
        selected: FaceBox | None,
        x_error: float | None,
        y_error: float | None,
        lock_frames: int,
        lock_needed: int,
        head_tilt: int,
        action: str,
        state: str,
    ) -> None:
        selected_index = next((index for index, face in enumerate(faces) if face is selected), None)
        payload = {
            "stamp": time.time(),
            "state": state,
            "faces": [asdict(face) for face in faces],
            "selected": selected_index,
            "x_error": x_error,
            "y_error": y_error,
            "lock_frames": lock_frames,
            "lock_needed": lock_needed,
            "head_tilt": head_tilt,
            "action": action,
            "x_tolerance": FACE_CENTER_X_TOLERANCE,
            "y_tolerance": FACE_CENTER_Y_TOLERANCE,
            "min_confidence": FACE_DETECTION_MIN_CONFIDENCE,
        }
        self._publisher.publish(String(data=json.dumps(payload, separators=(",", ":"))))


class GazeController:
    """Controls head tilt and wheel pan to track faces."""

    # Pan parameters (from original)
    PAN_GAIN = 0.4  # rad/s per unit offset
    PAN_COOLDOWN = 0.5  # seconds between pan adjustments
    PAN_THRESHOLD = 5.0  # degrees - only pan if error exceeds this

    def __init__(
        self,
        head_command_fn: Callable[[int], None],
        wheel_rotate_fn: Callable[[float, float], None] | None = None,
    ):
        self._head_command = head_command_fn
        self._wheel_rotate = wheel_rotate_fn

        self._target_tilt = 0.0
        self._last_commanded_tilt: int | None = None

        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._last_pan_time = 0.0

    def start(self):
        if self._running:
            return
        with self._lock:
            self._last_commanded_tilt = None  # re-command the held target after a pause
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def track_face(self, face: FaceBox):
        """Track a detected face by pointing at its center."""
        # Pan error (positive = face is on right = turn right)
        pan_error = (face.center_x - 0.5) * CAMERA_HFOV

        error_normalized = 0.5 - face.center_y
        if abs(error_normalized) > FACE_CENTER_Y_DEADZONE:
            with self._lock:
                self._target_tilt = clamp_tilt(self._target_tilt + error_normalized * CAMERA_VFOV * TILT_KP)

        # Execute pan if significant
        if abs(pan_error) > self.PAN_THRESHOLD:
            self._execute_pan(pan_error)

    def search_up(self):
        """One step toward the tilt where standing faces enter the frame."""
        with self._lock:
            if self._target_tilt < FACE_SEARCH_START_TILT:
                self._target_tilt = min(FACE_SEARCH_START_TILT, self._target_tilt + FACE_SEARCH_TILT_STEP)

    def _execute_pan(self, pan_degrees: float):
        """Execute pan via wheel rotation (rate limited)."""
        if not self._wheel_rotate:
            return

        now = time.time()
        if now - self._last_pan_time < self.PAN_COOLDOWN:
            return

        # Positive pan = face is right = rotate right (negative angular velocity)
        angular_speed = -math.copysign(self.PAN_GAIN, pan_degrees)
        duration = min(abs(pan_degrees) / 30.0, 0.5)  # Cap duration

        if duration > 0.05:
            self._wheel_rotate(angular_speed, duration)
            self._last_pan_time = now

    @property
    def target_tilt(self) -> int:
        with self._lock:
            return round(self._target_tilt)

    def _loop(self):
        """Main tilt control loop at ~30Hz."""
        dt = 1.0 / 30.0

        while self._running:
            loop_start = time.time()

            with self._lock:
                tilt_int = int(round(clamp_tilt(self._target_tilt)))
                last_commanded = self._last_commanded_tilt

            if last_commanded is None or tilt_int != last_commanded:
                self._head_command(tilt_int)
                with self._lock:
                    self._last_commanded_tilt = tilt_int

            elapsed = time.time() - loop_start
            if elapsed < dt:
                time.sleep(dt - elapsed)


class ROSPersonTracker:
    """ROS2 person tracker - simple interface for agents."""

    def __init__(self, node, camera_topic: str = "/mars/main_camera/left/image_raw/compressed"):
        self._node = node
        self._frame = None
        self._frame_lock = threading.Lock()

        # Hardware interfaces
        self._head = Head(node, node.get_logger())
        self._mobility = Mobility(node, node.get_logger(), "/cmd_vel")

        # Gaze controller
        self._gaze = GazeController(
            head_command_fn=self._head.set_position,
            wheel_rotate_fn=self._mobility.rotate_in_place,
        )
        self._detector: FaceDetector | None = None
        self._debug = FaceDebugPublisher(node)
        self._debug_lock_frames = 0
        self._last_face_time = time.time()

        self._running = False
        self._thread: threading.Thread | None = None

        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )
        self._sub = node.create_subscription(CompressedImage, camera_topic, self._on_image, qos)

    def _on_image(self, msg):
        if not msg.data:
            return
        frame = cv2.imdecode(np.frombuffer(msg.data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return
        with self._frame_lock:
            self._frame = frame

    def start(self):
        """Start person tracking."""
        if self._running:
            return
        self._running = True
        self._last_face_time = time.time()
        self._gaze.start()
        self._thread = threading.Thread(target=self._track_loop, daemon=True)
        self._thread.start()
        # Lazy init detector in background
        if self._detector is None:
            threading.Thread(target=self._init_detector, daemon=True).start()

    def _init_detector(self):
        try:
            self._detector = FaceDetector(min_confidence=FACE_DETECTION_MIN_CONFIDENCE)
            self._node.get_logger().info("👁️ Face detector initialized")
        except Exception as e:
            self._node.get_logger().error(f"Failed to init face detector: {e}")

    def stop(self):
        """Stop person tracking."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._gaze.stop()

    @property
    def is_running(self) -> bool:
        return self._running

    def _publish_debug(self, faces: list[FaceBox], selected: FaceBox | None) -> None:
        x_error = None if selected is None else selected.center_x - 0.5
        y_error = None if selected is None else 0.5 - selected.center_y
        if x_error is None or y_error is None:
            self._debug_lock_frames = 0
            action, state = "no_face_detected", "searching"
        elif abs(x_error) > FACE_CENTER_X_TOLERANCE:
            self._debug_lock_frames = 0
            action, state = "turn_needed", "tracking"
        elif abs(y_error) > FACE_CENTER_Y_DEADZONE:
            self._debug_lock_frames = 0
            action, state = "tilt_needed", "tracking"
        else:
            self._debug_lock_frames = min(FACE_LOCK_FRAMES, self._debug_lock_frames + 1)
            action = "centered"
            state = "locked" if self._debug_lock_frames >= FACE_LOCK_FRAMES else "tracking"
        self._debug.publish(
            faces=faces,
            selected=selected,
            x_error=x_error,
            y_error=y_error,
            lock_frames=self._debug_lock_frames,
            lock_needed=FACE_LOCK_FRAMES,
            head_tilt=self._gaze.target_tilt,
            action=action,
            state=state,
        )

    def _track_loop(self):
        """Perception loop at ~5Hz."""
        dt = 1.0 / FACE_TRACK_HZ

        while self._running:
            loop_start = time.time()

            if self._detector is None:
                time.sleep(0.1)
                continue

            with self._frame_lock:
                frame = self._frame

            if frame is not None:
                faces = self._detector.detect(frame)
                best = min(faces, key=lambda face: face.center_y) if faces else None
                if best is not None:
                    self._gaze.track_face(best)
                    self._last_face_time = time.time()
                elif time.time() - self._last_face_time > FACE_LOST_SECONDS:
                    self._gaze.search_up()
                self._publish_debug(faces, best)

            elapsed = time.time() - loop_start
            if elapsed < dt:
                time.sleep(dt - elapsed)
