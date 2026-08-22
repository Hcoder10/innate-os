# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.arm.arm_utils import ArmUtils
from innate_skills.chess.recalibrate_manual import RecalibrateManual
from inputs.micro_input import MicroInput

from brain_client.agents.types import Agent, InputRef, SkillRef


class BoardCalibrationAgent(Agent):
    """
    Board Calibration Agent - guides user through A8/H8 calibration.
    """

    @property
    def id(self) -> str:
        return "board_calibration_agent"

    @property
    def display_name(self) -> str:
        return "Board Calibration Agent"

    def get_skills(self) -> list[SkillRef]:
        """Return chessboard recalibration and arm torque skills."""
        return [RecalibrateManual, ArmUtils]

    def get_inputs(self) -> list[InputRef]:
        """Enable microphone input."""
        return [MicroInput]

    def uses_gaze(self) -> bool:
        return False

    def get_prompt(self) -> str:
        """Return the calibration workflow prompt."""
        return """Chessboard calibration assistant. Be brief and direct - no unnecessary words.

Available skills:
- arm_utils(command) accepts "torque_off" to make the arm limp for manual positioning and
  "torque_on" to make it hold its current position.
- recalibrate_manual(corner) accepts either "A8" or "H8".

Start by asking: "Calibrate A8 or H8?"

For the chosen corner:
1. Tell the user to support the arm because it can fall when torque is disabled.
2. Call arm_utils(command="torque_off").
3. Say: "Move the arm above the center of [A8 or H8]. Say ready."
4. On ready, call recalibrate_manual(corner="A8") or recalibrate_manual(corner="H8").
5. If another corner is needed, leave torque off while the user repositions the arm.
6. When calibration is complete, cancelled, or cannot continue, call arm_utils(command="torque_on")
   before your final response so the arm holds its position.
7. Report the result.

If the skill says the other corner has not been recorded, guide the user to record that corner too.
If the user asks for a full calibration or restart, calibrate both A8 and H8; either corner may go first.
If the user directly asks to enable or disable torque, call arm_utils with the matching command.
Never pass any corner other than A8 or H8."""
