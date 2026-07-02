# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time

from innate import Interface, InterfaceType, RobotState, RobotStateType, Skill, SkillResult

# Raw angular speeds we allow (rad/s). Slow on purpose: there is no
# obstacle awareness on this path (in-place, but tails can clip things).
MIN_SPEED = 0.2
MAX_SPEED = 1.0
DEFAULT_SPEED = 0.5


class TurnInPlace(Skill):
    """Turn in place by an angle, using odometry only -- no Nav2, no map.

    The cmd_vel twin of drive_straight: publishes raw angular velocity and
    closes the loop on odometry yaw, so it works even when navigation is down
    and never fails on planning. Positive angle turns left (counter-clockwise,
    ROS convention), negative turns right.
    """

    mobility = Interface(InterfaceType.MOBILITY)
    odom = RobotState(RobotStateType.LAST_ODOM)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "turn_in_place"

    def guidelines(self):
        return (
            "Turn the robot in place by angle_degrees: positive turns left "
            "(counter-clockwise), negative turns right. Uses odometry only -- no map or "
            "path planning. E.g. turn right 90 degrees -> angle_degrees=-90."
        )

    def execute(self, angle_degrees: float, speed: float = DEFAULT_SPEED):
        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE
        if angle_degrees == 0.0:
            return "Turned 0 degrees", SkillResult.SUCCESS
        yaw = self._yaw()
        if yaw is None:
            return "No odometry available", SkillResult.FAILURE

        self._cancelled = False
        target = abs(angle_degrees)
        velocity = math.copysign(min(max(abs(speed), MIN_SPEED), MAX_SPEED), angle_degrees)
        deadline = time.time() + math.radians(target) / abs(velocity) * 3.0 + 2.0

        # accumulate wrapped yaw deltas so multi-turn and the ±180° seam work
        turned = 0.0
        last_yaw = yaw
        while turned < target:
            if self._cancelled:
                self._stop()
                return f"Turn cancelled after {turned:.0f} degrees", SkillResult.CANCELLED
            if time.time() > deadline:
                self._stop()
                return f"Stuck: turned only {turned:.0f} of {target:.0f} degrees", SkillResult.FAILURE
            # duration acts as a deadman: if this loop dies, the base stops.
            self.mobility.send_cmd_vel(angular_z=velocity, duration=0.5)
            time.sleep(0.05)
            yaw = self._yaw()
            if yaw is not None:
                delta = (yaw - last_yaw + 180.0) % 360.0 - 180.0
                turned += abs(delta)
                last_yaw = yaw

        self._stop()
        direction = "left" if angle_degrees > 0 else "right"
        return f"Turned {turned:.0f} degrees {direction}", SkillResult.SUCCESS

    def cancel(self):
        self._cancelled = True
        self._stop()
        return "Turn cancelled"

    def _yaw(self):
        """Current heading in degrees from the odometry state, or None."""
        try:
            return float(self.odom["theta_degrees"])
        except (TypeError, KeyError):
            return None

    def _stop(self):
        if self.mobility is not None:
            self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
