# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time

from innate import Interface, InterfaceType, RobotState, RobotStateType, Skill, SkillResult
from pydantic import BaseModel

# Allowed base speeds (m/s). Slow on purpose: no obstacle avoidance here.
MIN_SPEED = 0.05
MAX_SPEED = 0.3
DEFAULT_SPEED = 0.15
# how long to wait for the first odom message after execute() starts
ODOM_WAIT_SEC = 2.0


class MoveResult(BaseModel):
    """Structured payload on .data for chaining callers."""

    traveled_m: float


class MoveStraight(Skill):
    """Move straight for a distance using raw cmd_vel closed on odometry --
    no Nav2, no map, and NO obstacle avoidance. Negative distance moves
    backward."""

    mobility = Interface(InterfaceType.MOBILITY)
    odom = RobotState(RobotStateType.LAST_ODOM)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancelled = False

    @property
    def name(self):
        return "move_straight"

    def guidelines(self):
        return (
            "Move the robot straight forward (positive distance, meters) or backward "
            "(negative distance) using odometry only -- no map or path planning, and no "
            "obstacle avoidance. Use for short moves in clear space; prefer "
            "navigate_to_position when a map position is needed."
        )

    def execute(self, distance: float, speed: float = DEFAULT_SPEED):
        try:
            return self._execute(distance, speed)
        finally:
            # reset on exit, not entry: an entry reset would erase a cancel
            # delivered while the server was still setting up the goal
            self._cancelled = False

    def _execute(self, distance: float, speed: float):
        if self.mobility is None:
            return "Mobility interface not available", SkillResult.FAILURE
        if distance == 0.0:
            return "Moved 0.0m", SkillResult.SUCCESS, MoveResult(traveled_m=0.0)
        start = self._wait_for_position()
        if self._cancelled:
            return "Move cancelled", SkillResult.CANCELLED
        if start is None:
            return "No odometry available", SkillResult.FAILURE

        target = abs(distance)
        velocity = math.copysign(min(max(abs(speed), MIN_SPEED), MAX_SPEED), distance)
        # generous time budget; if stuck, stop commanding motion instead of pushing forever
        deadline = time.time() + target / abs(velocity) * 3.0 + 2.0

        traveled = 0.0
        while traveled < target:
            if self._cancelled:
                self._stop()
                return f"Move cancelled after {traveled:.2f}m", SkillResult.CANCELLED
            if time.time() > deadline:
                self._stop()
                return f"Stuck: moved only {traveled:.2f}m of {target:.2f}m", SkillResult.FAILURE
            # duration acts as a deadman: if this loop dies, the base stops
            self.mobility.send_cmd_vel(linear_x=velocity, duration=0.5)
            time.sleep(0.1)
            current = self._position()
            if current is not None:
                traveled = math.dist(current, start)

        self._stop()
        direction = "forward" if distance > 0 else "backward"
        return f"Moved {traveled:.2f}m {direction}", SkillResult.SUCCESS, MoveResult(traveled_m=round(traveled, 3))

    def cancel(self):
        self._cancelled = True
        self._stop()
        return "Move cancelled"

    def _position(self):
        """Current (x, y) from the odometry robot state, or None if absent."""
        try:
            p = self.odom["pose"]["pose"]["position"]
            return (p["x"], p["y"])
        except (TypeError, KeyError):
            return None

    def _wait_for_position(self):
        """(x, y) once odometry arrives, or None after ODOM_WAIT_SEC."""
        deadline = time.time() + ODOM_WAIT_SEC
        while True:
            position = self._position()
            if position is not None or self._cancelled or time.time() > deadline:
                return position
            time.sleep(0.02)

    def _stop(self):
        if self.mobility is not None:
            self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
