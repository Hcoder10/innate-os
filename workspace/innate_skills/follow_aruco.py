# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Follow ArUco Skill -- lock onto the first ArUco marker seen through the main
camera and follow it with the base, steering by the marker's pixel position and
keeping distance from its apparent size. Seeing the stop marker (id 4) ends the
follow. No obstacle avoidance."""

import base64
import time

import cv2
import numpy as np
from innate import Interface, InterfaceType, RobotState, RobotStateType, Skill, SkillResult

# Must match the dictionary the tags were printed with (ids 0-4, DICT_4X4_50).
# The calibration ChArUco boards use DICT_4X4_250 -- a different dictionary.
ARUCO_DICT = cv2.aruco.DICT_4X4_50
STOP_ID = 4  # seeing this marker ends the follow
LOCK_IDS = {0, 1, 2, 3}  # the printed tag set minus the stop marker

# Background texture occasionally decodes as a valid marker for a frame, so a
# single detection is not trusted: locking and stopping both require the same
# id in consecutive frames.
LOCK_CONFIRM_FRAMES = 3
STOP_CONFIRM_FRAMES = 2

LOOP_PERIOD = 0.1  # control loop (s); camera state refreshes faster than this
CMD_DURATION = 0.4  # cmd_vel deadman: base stops if the loop dies

MAX_LINEAR = 0.3  # m/s forward
MAX_REVERSE = 0.1  # m/s backward when too close
MAX_ANGULAR = 0.8  # rad/s
TURN_GAIN = 1.5  # rad/s per unit of normalized horizontal offset
LINEAR_GAIN = 0.6  # m/s per unit of relative size error

# Follow distance is held by keeping the marker's apparent side length at this
# fraction of image width (~0.8 m for an 8 cm tag on the main camera).
TARGET_SIZE_FRAC = 0.07
SIZE_DEADBAND = 0.15  # relative size error below which we don't drive


class FollowAruco(Skill):
    """Follow the first ArUco marker seen until the stop marker appears."""

    mobility = Interface(InterfaceType.MOBILITY)
    image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)

    def __init__(self, logger):
        super().__init__(logger)
        dictionary = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
        self._detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())

    @property
    def name(self):
        return "follow_aruco"

    def guidelines(self):
        return (
            "Follow a person or object carrying an ArUco tag (DICT_4X4_50, ids 0-3). "
            "The robot waits until it sees one, locks onto that id, says 'Locked in', and "
            "then drives to keep that marker centered and about 0.8m away. Showing "
            f"marker id {STOP_ID} stops the follow. Runs until the stop marker is seen "
            "or the skill is cancelled. No obstacle avoidance -- use in clear space."
        )

    def execute(self):
        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE
        if not self._wait_for_frame():
            return "No camera image available", SkillResult.FAILURE

        locked_id = None
        lock_candidate = None
        lock_streak = 0
        stop_streak = 0
        while not self._cancelled:
            markers = self._detect_markers()

            stop_streak = stop_streak + 1 if STOP_ID in markers else 0
            if stop_streak >= STOP_CONFIRM_FRAMES:
                self._stop()
                self.say("Stopping")
                return f"Stop marker (id {STOP_ID}) seen, follow ended", SkillResult.SUCCESS

            if locked_id is None:
                seen = next((i for i in markers if i in LOCK_IDS), None)
                lock_streak = lock_streak + 1 if seen is not None and seen == lock_candidate else 1
                lock_candidate = seen
                if seen is not None and lock_streak >= LOCK_CONFIRM_FRAMES:
                    locked_id = seen
                    self.say("Locked in")
                    self._send_feedback(f"Locked onto marker id {locked_id}")
                else:
                    time.sleep(LOOP_PERIOD)
                    continue

            quad = markers.get(locked_id)
            if quad is None:
                self._stop()  # marker out of sight: hold position, keep scanning
            else:
                self._drive_toward(quad)
            time.sleep(LOOP_PERIOD)

        self._stop()
        return "Follow cancelled", SkillResult.CANCELLED

    def cancel(self):
        self._cancelled = True
        self._stop()
        return "Follow cancelled"

    def _detect_markers(self) -> dict:
        """Detected markers in the current frame as {id: quad of 4 (x, y) corners}."""
        frame = self._current_frame()
        if frame is None:
            return {}
        self._frame_width = frame.shape[1]
        corners, ids, _rejected = self._detector.detectMarkers(frame)
        if ids is None:
            return {}
        return {int(marker_id): quad.reshape(4, 2) for marker_id, quad in zip(ids.flatten(), corners)}

    def _current_frame(self):
        b64 = self.image
        if not b64:
            return None
        data = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)

    def _drive_toward(self, quad) -> None:
        # Steer: normalized horizontal offset of the marker center, [-1, 1].
        center_x = quad[:, 0].mean()
        offset = (center_x - self._frame_width / 2) / (self._frame_width / 2)
        # Positive angular_z is a left turn; marker right of center needs a right turn.
        angular = float(np.clip(-TURN_GAIN * offset, -MAX_ANGULAR, MAX_ANGULAR))

        # Distance: apparent side length vs. target. Too small -> approach,
        # too large -> back off.
        side_frac = np.mean([np.linalg.norm(quad[i] - quad[(i + 1) % 4]) for i in range(4)]) / self._frame_width
        size_error = 1.0 - side_frac / TARGET_SIZE_FRAC
        linear = 0.0
        if abs(size_error) > SIZE_DEADBAND:
            linear = float(np.clip(LINEAR_GAIN * size_error, -MAX_REVERSE, MAX_LINEAR))

        self.mobility.send_cmd_vel(linear_x=linear, angular_z=angular, duration=CMD_DURATION)

    def _wait_for_frame(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while self.image is None and not self._cancelled:
            if time.time() > deadline:
                return False
            time.sleep(0.05)
        return True

    def _stop(self):
        if self.mobility is not None:
            self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
