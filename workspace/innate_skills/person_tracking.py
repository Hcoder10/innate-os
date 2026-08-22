# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time

import cv2
import numpy as np

from brain_client.perception.gaze import (
    CAMERA_VFOV,
    FACE_CENTER_X_TOLERANCE,
    FACE_CENTER_Y_TOLERANCE,
    FACE_DETECTION_MIN_CONFIDENCE,
    FACE_LOCK_SECONDS,
    FACE_SEARCH_START_TILT,
    FACE_SEARCH_TILT_STEP,
    MAX_TILT,
    MIN_TILT,
    TILT_KP,
    FaceBox,
    FaceDebugPublisher,
    FaceDetector,
)
from innate import Head, HeadState, MainImage, Mobility, Skill, resource

TURN_SPEED = 0.35
TURN_DURATION = 0.25
TRACK_PERIOD = 0.15
FACE_LOCK_FRAMES = math.ceil(FACE_LOCK_SECONDS / TRACK_PERIOD)


class _PersonTrackingSkill(Skill):
    head: Head
    head_position: HeadState | None
    image: MainImage | None
    mobility: Mobility

    @resource
    def _face_detector(self) -> FaceDetector:
        return FaceDetector(min_confidence=FACE_DETECTION_MIN_CONFIDENCE)

    @resource
    def _face_debug(self) -> FaceDebugPublisher:
        node = self.node
        if node is None:
            raise RuntimeError("Face diagnostics require a wired skill node")
        return FaceDebugPublisher(node)

    def _visible_faces(self) -> list[FaceBox]:
        image = self.image
        if image is None:
            return []
        frame = cv2.imdecode(np.frombuffer(image.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return []
        return self._face_detector.detect(frame)

    def _in_tolerance(self, face: FaceBox) -> bool:
        return (
            abs(face.center_x - 0.5) <= FACE_CENTER_X_TOLERANCE and abs(0.5 - face.center_y) <= FACE_CENTER_Y_TOLERANCE
        )

    def _select_face(self, faces: list[FaceBox], side: str = "unknown") -> FaceBox | None:
        if not faces:
            return None
        candidates = [face for face in faces if self._in_tolerance(face)] or faces
        target_x = {"left": 0.2, "right": 0.8, "center": 0.5}.get(side)
        if target_x is not None:
            return min(candidates, key=lambda face: abs(face.center_x - target_x))
        return min(candidates, key=lambda face: face.center_y)

    def _center_face(
        self, side: str = "unknown", timeout: float = 10.0, *, look_up: bool = False
    ) -> tuple[int, FaceBox] | None:
        current_tilt = self._head_angle()
        target_tilt = max(current_tilt, FACE_SEARCH_START_TILT) if look_up else current_tilt
        if look_up:
            self.head.set_position(target_tilt)
        self._face_debug.publish(
            faces=[],
            selected=None,
            x_error=None,
            y_error=None,
            lock_frames=0,
            lock_needed=FACE_LOCK_FRAMES,
            head_tilt=target_tilt,
            action="waiting_for_camera",
            state="starting",
        )
        if self.wait_for(lambda: self.image, timeout=5.0) is None:
            self.logger.warning("[PersonTracking] No camera image received within 5.0s")
            self._face_debug.publish(
                faces=[],
                selected=None,
                x_error=None,
                y_error=None,
                lock_frames=0,
                lock_needed=FACE_LOCK_FRAMES,
                head_tilt=target_tilt,
                action="no_camera_image",
                state="failed",
            )
            return None

        deadline = time.monotonic() + timeout
        locked_frames = 0
        best_lock_frames = 0
        detected_frames = 0
        faces: list[FaceBox] = []
        x_error: float | None = None
        y_error: float | None = None
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
                    x_error = None
                    y_error = None
                    locked_frames = 0
                    previous_tilt = target_tilt
                    target_tilt = min(MAX_TILT, target_tilt + FACE_SEARCH_TILT_STEP)
                    self.head.set_position(target_tilt)
                    action = "search_up" if target_tilt > previous_tilt else "upper_limit"
                    self._face_debug.publish(
                        faces=faces,
                        selected=None,
                        x_error=None,
                        y_error=None,
                        lock_frames=0,
                        lock_needed=FACE_LOCK_FRAMES,
                        head_tilt=target_tilt,
                        action=action,
                        state="searching",
                    )
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
                        MIN_TILT,
                        min(MAX_TILT, target_tilt + y_error * CAMERA_VFOV * TILT_KP),
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
                state = "locked" if locked_frames >= FACE_LOCK_FRAMES else "tracking"
                self._face_debug.publish(
                    faces=faces,
                    selected=face,
                    x_error=x_error,
                    y_error=y_error,
                    lock_frames=locked_frames,
                    lock_needed=FACE_LOCK_FRAMES,
                    head_tilt=target_tilt,
                    action=action,
                    state=state,
                )
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
        self._face_debug.publish(
            faces=faces,
            selected=tracked,
            x_error=x_error,
            y_error=y_error,
            lock_frames=locked_frames,
            lock_needed=FACE_LOCK_FRAMES,
            head_tilt=target_tilt,
            action=reason.replace(" ", "_"),
            state="failed",
        )
        return None

    def _tracked_face(self, faces: list[FaceBox], tracked: FaceBox | None, side: str) -> FaceBox | None:
        if not faces:
            return None
        if tracked is None:
            return self._select_face(faces, side)
        candidates = [face for face in faces if self._in_tolerance(face)] or faces
        return min(
            candidates,
            key=lambda face: (
                abs(face.center_x - tracked.center_x)
                + abs(face.center_y - tracked.center_y)
                + abs(face.width - tracked.width)
            ),
        )

    def _head_angle(self) -> int:
        state = self.head_position
        return 0 if state is None else round(state.pitch_degrees)
