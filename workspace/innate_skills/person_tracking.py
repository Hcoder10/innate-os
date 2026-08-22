# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time
from dataclasses import dataclass

import cv2
import numpy as np

from brain_client.perception.gaze import FaceDetector
from innate import Head, HeadState, MainImage, Mobility, Skill, resource

FACE_LOCK_FRAMES = 2
FACE_CENTER_X_TOLERANCE = 0.08
FACE_CENTER_Y_TOLERANCE = 0.10
CAMERA_VERTICAL_FOV = 50.0
TILT_GAIN = 0.3
MIN_HEAD_TILT = -25
MAX_HEAD_TILT = 15
FACE_SEARCH_TILT_STEP = 3
TURN_SPEED = 0.35
TURN_DURATION = 0.25
TRACK_PERIOD = 0.15


@dataclass(frozen=True)
class FaceBox:
    center_x: float
    center_y: float
    width: float
    height: float


class _PersonTrackingSkill(Skill):
    head: Head
    head_position: HeadState | None
    image: MainImage | None
    mobility: Mobility

    @resource
    def _face_detector(self) -> FaceDetector:
        return FaceDetector(min_confidence=0.3)

    def _visible_faces(self) -> list[FaceBox]:
        image = self.image
        if image is None:
            return []
        frame = cv2.imdecode(np.frombuffer(image.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return []
        return [FaceBox(**face) for face in self._face_detector.detect(frame)]

    def _select_face(self, faces: list[FaceBox], side: str = "unknown") -> FaceBox | None:
        if not faces:
            return None
        target_x = {"left": 0.2, "right": 0.8, "center": 0.5}.get(side)
        if target_x is not None:
            return min(faces, key=lambda face: abs(face.center_x - target_x))
        return max(faces, key=lambda face: face.width * face.height)

    def _center_face(self, side: str = "unknown", timeout: float = 6.0) -> tuple[int, FaceBox] | None:
        if self.wait_for(lambda: self.image, timeout=5.0) is None:
            self.logger.warning("[PersonTracking] No camera image received within 5.0s")
            return None

        deadline = time.monotonic() + timeout
        target_tilt = self._head_angle()
        locked_frames = 0
        best_lock_frames = 0
        detected_frames = 0
        tracked: FaceBox | None = None
        self.logger.info(
            f"[PersonTracking] Centering started: side={side}, timeout={timeout:.1f}s, "
            f"x_tolerance={FACE_CENTER_X_TOLERANCE:.2f}, y_tolerance={FACE_CENTER_Y_TOLERANCE:.2f}"
        )
        try:
            while time.monotonic() < deadline:
                faces = self._visible_faces()
                face = self._tracked_face(faces, tracked, side)
                if face is None:
                    tracked = None
                    locked_frames = 0
                    previous_tilt = target_tilt
                    target_tilt = min(MAX_HEAD_TILT, target_tilt + FACE_SEARCH_TILT_STEP)
                    self.head.set_position(target_tilt)
                    action = "search_up" if target_tilt > previous_tilt else "upper_limit"
                    self.logger.info(
                        f"[PersonTracking] faces=0 selected=none lock=0 head={target_tilt} action={action}"
                    )
                    self.sleep(TRACK_PERIOD)
                    continue

                detected_frames += 1
                tracked = face
                x_error = face.center_x - 0.5
                y_error = 0.5 - face.center_y
                target_tilt = round(
                    max(
                        MIN_HEAD_TILT,
                        min(MAX_HEAD_TILT, target_tilt + y_error * CAMERA_VERTICAL_FOV * TILT_GAIN),
                    )
                )
                self.head.set_position(target_tilt)

                if abs(x_error) > FACE_CENTER_X_TOLERANCE:
                    angular = -math.copysign(TURN_SPEED, x_error)
                    self.mobility.send_cmd_vel(angular_z=angular, duration=TURN_DURATION)
                    locked_frames = 0
                    action = f"turn angular_z={angular:.2f}"
                elif abs(y_error) > FACE_CENTER_Y_TOLERANCE:
                    locked_frames = 0
                    action = f"tilt head={target_tilt}"
                else:
                    locked_frames += 1
                    best_lock_frames = max(best_lock_frames, locked_frames)
                    action = f"lock={locked_frames}/{FACE_LOCK_FRAMES}"
                self.logger.info(
                    f"[PersonTracking] faces={len(faces)} center=({face.center_x:.3f},{face.center_y:.3f}) "
                    f"size=({face.width:.3f},{face.height:.3f}) error=({x_error:+.3f},{y_error:+.3f}) "
                    f"head={target_tilt} action={action}"
                )
                if locked_frames >= FACE_LOCK_FRAMES:
                    self.logger.info(
                        f"[PersonTracking] Face locked after {detected_frames} detected frames at "
                        f"center=({face.center_x:.3f},{face.center_y:.3f})"
                    )
                    return target_tilt, face
                self.sleep(TRACK_PERIOD)
        finally:
            self.mobility.stop()
        reason = "no face was detected" if detected_frames == 0 else "face never held both center tolerances"
        self.logger.warning(
            f"[PersonTracking] Centering timed out: {reason}; "
            f"detected_frames={detected_frames}, best_lock={best_lock_frames}/{FACE_LOCK_FRAMES}"
        )
        return None

    def _tracked_face(self, faces: list[FaceBox], tracked: FaceBox | None, side: str) -> FaceBox | None:
        if not faces:
            return None
        if tracked is None:
            return self._select_face(faces, side)
        return min(
            faces,
            key=lambda face: (
                abs(face.center_x - tracked.center_x)
                + abs(face.center_y - tracked.center_y)
                + abs(face.width - tracked.width)
            ),
        )

    def _head_angle(self) -> int:
        state = self.head_position
        return 0 if state is None else round(state.pitch_degrees)
