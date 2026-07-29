# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import time

import cv2
import numpy as np

from innate import MainImage, Mobility, Skill, SkillResult, SkillReturn

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
LINEAR_GAIN = 0.8  # m/s per unit of relative size error past the deadband

# Follow distance is held by keeping the marker's apparent side length at this
# fraction of image width (~0.35 m for an 8 cm tag on the main camera).
TARGET_SIZE_FRAC = 0.16
SIZE_DEADBAND = 0.15  # relative size error below which we don't drive

# Smoothing: per-frame corner jitter would otherwise feed straight into the
# command, and step changes in cmd_vel jerk the base.
MEAS_SMOOTHING = 0.35  # EMA weight of the newest measurement, (0, 1]
LINEAR_SLEW = 0.5  # m/s^2 max change in commanded linear velocity
ANGULAR_SLEW = 2.0  # rad/s^2 max change in commanded angular velocity
LOST_GRACE_FRAMES = 5  # ramp down this many missed frames before a hard stop


class FollowAruco(Skill):
    """Follow a person or object carrying an ArUco tag."""

    mobility: Mobility
    # ``| None``: the required-feed grace (3 s) is too tight for a cold camera
    # (sim renders first frames on demand); execute() waits up to 5 s itself
    # before giving up, as the pre-annotation skill did.
    image: MainImage | None

    def guidelines(self) -> str:
        # generated from LOCK_IDS/STOP_ID so retuning them can't desync the prose
        ids = ", ".join(str(i) for i in sorted(LOCK_IDS))
        return (
            f"Follow a person or object carrying an ArUco tag (DICT_4X4_50, ids {ids}). "
            "The robot waits until it sees one, locks onto that id, says 'Locked in', and "
            "then drives to keep that marker centered and about 0.35m away. Showing "
            f"marker id {STOP_ID} stops the follow. Runs until the stop marker is seen or the "
            "skill is cancelled. No obstacle avoidance -- use in clear space."
        )

    def __init__(self, logger):
        super().__init__(logger)
        dictionary = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
        self._detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
        self._frame_width = 1  # real width set on every successful _detect_markers() decode
        self._offset_filtered = None
        self._size_error_filtered = None
        self._cmd_linear = 0.0
        self._cmd_angular = 0.0
        self._last_cmd_time = None

    def execute(self) -> SkillReturn:
        self.on_cancel(self._stop)  # brake now, not at the next loop poll
        if self.wait_for(lambda: self.image, timeout=5.0) is None:
            return "No camera image available", SkillResult.FAILURE
        locked_id = None
        lock_candidate = None
        lock_streak = 0
        stop_streak = 0
        lost_frames = 0
        while not self.cancelled:
            markers = self._detect_markers()

            stop_streak = stop_streak + 1 if STOP_ID in markers else 0
            if stop_streak >= STOP_CONFIRM_FRAMES:
                self._stop()
                self.say("Stopping")
                return f"Stop marker (id {STOP_ID}) seen, follow ended"

            if locked_id is None:
                seen = next((i for i in markers if i in LOCK_IDS), None)
                lock_streak = lock_streak + 1 if seen is not None and seen == lock_candidate else 1
                lock_candidate = seen
                if seen is not None and lock_streak >= LOCK_CONFIRM_FRAMES:
                    locked_id = seen
                    self.say("Locked in")
                    self.feedback(f"Locked onto marker id {locked_id}")
                else:
                    time.sleep(LOOP_PERIOD)
                    continue

            quad = markers.get(locked_id)
            if quad is None:
                lost_frames += 1
                # Drop stale measurements so a re-acquired marker that moved
                # during the miss doesn't steer off the old filter state.
                self._offset_filtered = None
                self._size_error_filtered = None
                if lost_frames > LOST_GRACE_FRAMES:
                    self._stop()  # marker out of sight: hold position, keep scanning
                else:
                    self._send_cmd(0.0, 0.0)  # brief detection miss: ramp down, don't jerk
            else:
                lost_frames = 0
                self._drive_toward(quad)
            time.sleep(LOOP_PERIOD)

        self._stop()
        return "Follow cancelled", SkillResult.CANCELLED

    def _detect_markers(self) -> dict:
        """Detected markers in the current frame as {id: quad of 4 (x, y) corners}."""
        frame = self._current_frame()
        if frame is None:
            return {}
        self._frame_width = frame.shape[1]
        corners, ids, _rejected = self._detector.detectMarkers(frame)
        if ids is None:
            return {}
        return {int(marker_id): quad.reshape(4, 2) for marker_id, quad in zip(ids.flatten(), corners, strict=True)}

    def _current_frame(self):
        frame = self.image
        if not frame:
            return None
        data = np.frombuffer(frame.jpeg, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)

    def _drive_toward(self, quad) -> None:
        # Steer: normalized horizontal offset of the marker center, [-1, 1].
        center_x = quad[:, 0].mean()
        offset = (center_x - self._frame_width / 2) / (self._frame_width / 2)
        # Distance: apparent side length vs. target. Too small -> approach,
        # too large -> back off.
        side_frac = np.mean([np.linalg.norm(quad[i] - quad[(i + 1) % 4]) for i in range(4)]) / self._frame_width
        size_error = 1.0 - side_frac / TARGET_SIZE_FRAC

        self._offset_filtered = self._smooth(self._offset_filtered, offset)
        self._size_error_filtered = self._smooth(self._size_error_filtered, size_error)

        # Positive angular_z is a left turn; marker right of center needs a right turn.
        angular = float(np.clip(-TURN_GAIN * self._offset_filtered, -MAX_ANGULAR, MAX_ANGULAR))

        linear = 0.0
        err = self._size_error_filtered
        if abs(err) > SIZE_DEADBAND:
            # Ramp from zero at the deadband edge instead of jumping to full gain.
            past_deadband = err - np.copysign(SIZE_DEADBAND, err)
            linear = float(np.clip(LINEAR_GAIN * past_deadband, -MAX_REVERSE, MAX_LINEAR))

        self._send_cmd(linear, angular)

    @staticmethod
    def _smooth(filtered, measurement):
        if filtered is None:
            return measurement
        return filtered + MEAS_SMOOTHING * (measurement - filtered)

    def _send_cmd(self, linear, angular) -> None:
        # A cancel mid-iteration already braked via the on_cancel hook;
        # re-commanding here would undo it until the next loop check.
        if self.cancelled:
            return
        # Slew-limit toward the requested velocities so the base accelerates
        # and decelerates gradually. The step scales with real elapsed time, so
        # a slow loop iteration doesn't silently lower the slew rate. dt is
        # capped at CMD_DURATION: past that the deadman has stopped the base,
        # and a bigger step would jerk it from standstill.
        now = time.monotonic()
        dt = min(now - self._last_cmd_time, CMD_DURATION) if self._last_cmd_time is not None else LOOP_PERIOD
        self._last_cmd_time = now
        self._cmd_linear += float(np.clip(linear - self._cmd_linear, -LINEAR_SLEW * dt, LINEAR_SLEW * dt))
        self._cmd_angular += float(np.clip(angular - self._cmd_angular, -ANGULAR_SLEW * dt, ANGULAR_SLEW * dt))
        self.mobility.send_cmd_vel(linear_x=self._cmd_linear, angular_z=self._cmd_angular, duration=CMD_DURATION)

    def _stop(self):
        self._offset_filtered = None
        self._size_error_filtered = None
        self._cmd_linear = 0.0
        self._cmd_angular = 0.0
        self._last_cmd_time = None
        self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
