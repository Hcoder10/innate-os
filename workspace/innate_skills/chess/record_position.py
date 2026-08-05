# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from innate import Manipulation, Skill, SkillReturn, WristImage

CALIBRATION_FILE = Path.home() / "board_calibration.json"
CORNER_CAPTURES_DIR = Path("/home/jetson1/innate-os/captures/corners")
BoardCorner = Literal["top_left", "top_right", "bottom_right", "bottom_left"]
VALID_CORNERS = ("top_left", "top_right", "bottom_right", "bottom_left")


class RecordPosition(Skill):
    """Record the current arm position for a board corner. Requires 'corner'
    parameter: 'top_left', 'top_right', 'bottom_right', or 'bottom_left'.
    Saves to calibration file and returns coordinates."""

    manipulation: Manipulation
    image: WristImage | None  # debug snapshot only; missing frame must not abort

    def execute(self, corner: BoardCorner) -> SkillReturn:
        corner = cast(BoardCorner, corner.lower().replace("-", "_").replace(" ", "_"))
        if corner not in VALID_CORNERS:
            self.fail(f"Invalid corner '{corner}'. Must be one of: {VALID_CORNERS}")

        fk_pose = self.manipulation.get_current_end_effector_pose()
        if not fk_pose:
            self.fail("Could not get current position")
        pos = fk_pose["position"]

        calibration = {}
        if CALIBRATION_FILE.exists():
            try:
                calibration = json.loads(CALIBRATION_FILE.read_text())
            except Exception:
                calibration = {}
        calibration[corner] = {"x": pos["x"], "y": pos["y"], "z": pos["z"]}
        CALIBRATION_FILE.write_text(json.dumps(calibration, indent=2))

        self._save_corner_image(corner)

        position_str = f"X={pos['x']:.4f}, Y={pos['y']:.4f}, Z={pos['z']:.4f}"
        self.feedback(f"RECORDED {corner.upper()}: {position_str}")
        return f"{corner} recorded: {position_str}"

    def _save_corner_image(self, corner: str):
        if not self.image:
            self.logger.warning("No wrist camera image available to save")
            return
        try:
            CORNER_CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            (CORNER_CAPTURES_DIR / f"corner_{corner}_{ts}.jpg").write_bytes(self.image.jpeg)
        except Exception as e:
            self.logger.warning(f"Failed to save corner image: {e}")
