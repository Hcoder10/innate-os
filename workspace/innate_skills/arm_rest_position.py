#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Arm Rest Position Skill — move the arm to its resting pose.

Callable from any skill's code as a plain function:

    from innate.skills import arm_rest_position
    arm_rest_position()            # blocks until the arm is resting

(also runnable from the agent, the webapp skills menu, and
`scripts/innate skill run local/arm_rest_position`).
"""

import time

from brain_client.skills.types import (
    Interface,
    InterfaceType,
    RobotState,
    RobotStateType,
    Skill,
    SkillResult,
)

# joints 1-6 = base yaw, shoulder, elbow, wrist pitch, wrist roll, gripper.
# Folded rest shape. j4=0.30 keeps the gripper off the floor (ee_link z ~0.042 m);
# pitching it further down jams the fingers into the ground and trips a servo.
# These are the values the arm reaches and HOLDS — j1/j2 sit at their limits, so
# commanding "more folded" just settles here.
REST_POSITION = [1.5708, -1.2195, 1.5723, 0.30, 0.0, 0.0031]


class ArmRestPosition(Skill):
    """Move the arm to the resting position."""

    manipulation = Interface(InterfaceType.MANIPULATION)
    joint_states = RobotState(RobotStateType.LAST_JOINT_STATES)

    @property
    def name(self):
        return "arm_rest_position"

    def guidelines(self):
        return (
            "Use this to move the arm to its resting position (folded against "
            "the body, servos unloaded). Safe while holding an object: the "
            "gripper keeps its current closure unless keep_gripper=False."
        )

    def execute(self, duration: int = 3, keep_gripper: bool = True):
        """
        Move the arm to the resting pose.

        Args:
            duration: Trajectory duration in seconds.
            keep_gripper: Keep the gripper's current closure (default) so a
                held object isn't released; False also restores the captured
                rest gripper value.
        """
        self._cancelled = False

        if self.manipulation is None:
            return "Manipulation interface not available", SkillResult.FAILURE

        target = list(REST_POSITION)
        if keep_gripper:
            js = self.joint_states
            try:
                target[5] = float(js["position"][5])
            except (KeyError, IndexError, TypeError):
                pass  # no reading — fall back to the captured gripper value

        self.logger.info(f"Moving arm to rest position {[round(j, 3) for j in target]} over {duration}s")
        success = self.manipulation.move_to_joint_positions(joint_positions=target, duration=duration, blocking=False)
        if not success:
            return "Failed to send arm command", SkillResult.FAILURE

        start_time = time.time()
        while time.time() - start_time < duration:
            if self._cancelled:
                return "Arm motion cancelled", SkillResult.CANCELLED
            time.sleep(0.1)

        return "Arm moved to rest position", SkillResult.SUCCESS

    def cancel(self):
        """Cancel the arm movement."""
        self._cancelled = True
        return "Arm motion cancelled"
