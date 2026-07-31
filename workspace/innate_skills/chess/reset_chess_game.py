# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
from pathlib import Path
from typing import Literal, cast

from innate import Skill, SkillReturn

GAME_STATE_FILE = Path.home() / "chess_game_state.json"
CALIBRATION_FILE = Path.home() / "board_calibration.json"
REQUIRED_CORNERS = ("top_left", "top_right", "bottom_right", "bottom_left")
ROBOT_COLORS = ("white", "black")
RobotColor = Literal["white", "black"]

# Handicap: White starts without the a1 rook (no queenside castling)
HANDICAP_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/1NBQKBNR w Kkq - 0 1"


class ResetChessGame(Skill):
    """Reset the chess game to the starting position. Requires board
    calibration to be present first. Clears the move history and sets the
    board to the initial FEN. Optionally pass robot_color ('white' or 'black')
    to set which side the robot plays."""

    def _is_calibrated(self) -> bool:
        try:
            calibration = json.loads(CALIBRATION_FILE.read_text())
            for corner in REQUIRED_CORNERS:
                for axis in "xyz":
                    float(calibration[corner][axis])
            return True
        except Exception:
            return False

    def execute(self, robot_color: RobotColor = "white") -> SkillReturn:
        robot_color = cast(RobotColor, robot_color.strip().lower())
        if robot_color not in ROBOT_COLORS:
            self.fail(f"Invalid robot_color '{robot_color}'. Must be 'white' or 'black'.")
        if not self._is_calibrated():
            self.fail("Board is not calibrated. Run board calibration first, then reset the chess game.")

        state = {
            "fen": HANDICAP_FEN,
            "move_history": [],
            "last_detected_move": None,
            "turn": "white",
            "robot_color": robot_color,
        }
        try:
            GAME_STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e:
            self.fail(f"Failed to write game state: {e}")

        msg = f"Game reset to starting position. Robot plays {robot_color}."
        self.feedback(msg)
        return msg
