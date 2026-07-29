# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time

from pydantic import BaseModel

from innate import Mobility, Odometry, Skill, SkillResult, SkillReturn

# Allowed base speeds (m/s). Slow on purpose: no obstacle avoidance here.
MIN_SPEED = 0.05
MAX_SPEED = 0.3
DEFAULT_SPEED = 0.15


class MoveResult(BaseModel):
    """Structured payload on .data for chaining callers."""

    traveled_m: float


class MoveStraight(Skill):
    """Move the robot straight forward (positive distance, meters) or backward
    (negative distance) using odometry only -- no map or path planning, and no
    obstacle avoidance. Use for short moves in clear space; prefer
    navigate_to_position when a map position is needed."""

    mobility: Mobility
    odom: Odometry  # required: guaranteed before execute() starts

    def execute(self, distance: float, speed: float = DEFAULT_SPEED) -> SkillReturn:
        self.on_cancel(self._stop)  # brake now, not at the next poll

        if distance == 0.0:
            return "Moved 0.0m", SkillResult.SUCCESS, MoveResult(traveled_m=0.0)
        start = self.odom.position
        target = abs(distance)
        velocity = math.copysign(min(max(abs(speed), MIN_SPEED), MAX_SPEED), distance)
        # generous time budget; if stuck, stop commanding motion instead of pushing forever
        deadline = time.time() + target / abs(velocity) * 3.0 + 2.0

        traveled = 0.0
        while traveled < target:
            if self.cancelled:
                self._stop()
                return f"Move cancelled after {traveled:.2f}m", SkillResult.CANCELLED
            if time.time() > deadline:
                self._stop()
                self.fail(f"Stuck: moved only {traveled:.2f}m of {target:.2f}m")
            # duration acts as a deadman: if this loop dies, the base stops
            self.mobility.send_cmd_vel(linear_x=velocity, duration=0.5)
            if self.cancelled:
                # the on_cancel brake fired between the check above and the
                # send, which re-commanded motion — undo it now, not in 0.1 s
                self._stop()
                return f"Move cancelled after {traveled:.2f}m", SkillResult.CANCELLED
            time.sleep(0.1)
            traveled = math.dist(self.odom.position, start)

        self._stop()
        direction = "forward" if distance > 0 else "backward"
        return f"Moved {traveled:.2f}m {direction}", SkillResult.SUCCESS, MoveResult(traveled_m=round(traveled, 3))

    def _stop(self):
        self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
