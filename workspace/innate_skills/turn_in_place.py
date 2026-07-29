# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import time

from pydantic import BaseModel

from innate import Mobility, Odometry, Skill, SkillResult, SkillReturn

# Allowed angular speeds (rad/s). Slow on purpose: no obstacle awareness here.
MIN_SPEED = 0.2
MAX_SPEED = 1.0
DEFAULT_SPEED = 0.5


class TurnResult(BaseModel):
    """Structured payload on .data for chaining callers."""

    turned_degrees: float


class TurnInPlace(Skill):
    """Turn the robot in place by angle_degrees: positive turns left
    (counter-clockwise), negative turns right. Uses odometry only -- no map or
    path planning. E.g. turn right 90 degrees -> angle_degrees=-90."""

    mobility: Mobility
    odom: Odometry  # required: guaranteed before execute() starts

    def execute(self, angle_degrees: float, speed: float = DEFAULT_SPEED) -> SkillReturn:
        self.on_cancel(self._stop)  # brake now, not at the next poll

        if angle_degrees == 0.0:
            return "Turned 0 degrees", SkillResult.SUCCESS, TurnResult(turned_degrees=0.0)
        yaw = self.odom.theta_degrees
        target = abs(angle_degrees)
        sign = 1.0 if angle_degrees > 0 else -1.0
        velocity = math.copysign(min(max(abs(speed), MIN_SPEED), MAX_SPEED), angle_degrees)
        deadline = time.time() + math.radians(target) / abs(velocity) * 3.0 + 2.0

        # accumulate wrapped yaw deltas so multi-turn and the ±180° seam work;
        # signed so motion against the commanded direction subtracts
        turned = 0.0
        last_yaw = yaw
        while turned < target:
            if self.cancelled:
                self._stop()
                return f"Turn cancelled after {turned:.0f} degrees", SkillResult.CANCELLED
            if time.time() > deadline:
                self._stop()
                self.fail(f"Stuck: turned only {turned:.0f} of {target:.0f} degrees")
            # duration acts as a deadman: if this loop dies, the base stops
            self.mobility.send_cmd_vel(angular_z=velocity, duration=0.5)
            if self.cancelled:
                # the on_cancel brake fired between the check above and the
                # send, which re-commanded motion — undo it now, not in 0.05 s
                self._stop()
                return f"Turn cancelled after {turned:.0f} degrees", SkillResult.CANCELLED
            time.sleep(0.05)
            yaw = self.odom.theta_degrees
            delta = (yaw - last_yaw + 180.0) % 360.0 - 180.0
            turned += delta * sign
            last_yaw = yaw

        self._stop()
        direction = "left" if angle_degrees > 0 else "right"
        return (
            f"Turned {turned:.0f} degrees {direction}",
            SkillResult.SUCCESS,
            TurnResult(turned_degrees=round(turned, 1)),
        )

    def _stop(self):
        self.mobility.send_cmd_vel(linear_x=0.0, angular_z=0.0)
