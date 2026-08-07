# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from innate import Manipulation, Skill, SkillReturn, WristImage
from innate.exceptions import ArmFailed

CALIBRATION_FILE = Path.home() / "board_calibration.json"
CAPTURES_DIR = Path.home() / "innate-os/captures/corners"
CalibrationCorner = Literal["A8", "H8"]


class RecalibrateManual(Skill):
    """Manually recalibrate one top corner of the chessboard. The human
    positions the arm above the center of A8 or H8, then this skill records
    the position and recomputes the full board calibration using square
    geometry. Requires 'corner' parameter: 'A8' or 'H8'."""

    manipulation: Manipulation
    image: WristImage | None  # debug snapshot only; missing frame must not abort

    def _load_calibration(self):
        if not CALIBRATION_FILE.exists():
            return {}
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except Exception:
            return {}

    def _recompute_from_top_corners(self, new_a8, new_h8, z):
        """The board is a square: the bottom edge is the top edge (A8->H8)
        rotated 90 deg clockwise, toward the robot."""
        side_x = new_h8[0] - new_a8[0]
        side_y = new_h8[1] - new_a8[1]
        down_x, down_y = side_y, -side_x
        return {
            "top_left": {"x": new_a8[0], "y": new_a8[1], "z": z},
            "top_right": {"x": new_h8[0], "y": new_h8[1], "z": z},
            "bottom_left": {"x": new_a8[0] + down_x, "y": new_a8[1] + down_y, "z": z},
            "bottom_right": {"x": new_h8[0] + down_x, "y": new_h8[1] + down_y, "z": z},
        }

    def _save_corner_image(self, corner: str):
        if not self.image:
            self.logger.warning("[RecalibrateManual] No wrist camera image available to save")
            return
        try:
            CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            (CAPTURES_DIR / f"manual_{corner}_{ts}.jpg").write_bytes(self.image.jpeg)
        except Exception as e:
            self.logger.warning(f"[RecalibrateManual] Failed to save corner image: {e}")

    def _save_calibration(self, calibration):
        try:
            CALIBRATION_FILE.write_text(json.dumps(calibration, indent=2))
        except Exception as e:
            self.fail(f"Failed to save calibration: {e}")

    def execute(self, corner: CalibrationCorner) -> SkillReturn:
        corner = cast(CalibrationCorner, corner.upper().strip())
        if corner not in ("A8", "H8"):
            self.fail(f"Invalid corner '{corner}'. Must be 'A8' or 'H8'.")

        this_key, other_key, other_name = (
            ("top_left", "top_right", "H8") if corner == "A8" else ("top_right", "top_left", "A8")
        )
        calibration = self._load_calibration()

        try:
            pose = self.manipulation.pose
        except ArmFailed:
            self.fail("Could not get current arm position")
        pos = {"x": pose.x, "y": pose.y, "z": pose.z}
        position_str = f"X={pos['x']:.4f}, Y={pos['y']:.4f}, Z={pos['z']:.4f}"
        self.feedback(f"Recording {corner} at {position_str}")
        self._save_corner_image(corner)

        if other_key not in calibration:
            calibration[this_key] = {"x": pos["x"], "y": pos["y"], "z": pos["z"]}
            self._save_calibration(calibration)
            msg = (
                f"Recorded {corner} at {position_str}. "
                f"Other corner {other_name} not yet recorded — record it to compute full board."
            )
            self.feedback(msg)
            return msg

        other = calibration[other_key]
        new_pos = (pos["x"], pos["y"])
        other_pos = (other["x"], other["y"])
        new_a8, new_h8 = (new_pos, other_pos) if corner == "A8" else (other_pos, new_pos)
        cal_z = calibration.get("top_right", calibration.get("top_left", {})).get("z", pos["z"])
        self._save_calibration(self._recompute_from_top_corners(new_a8, new_h8, cal_z))

        side_len = math.hypot(new_h8[0] - new_a8[0], new_h8[1] - new_a8[1])
        msg = (
            f"Recorded {corner} at {position_str}. "
            f"Recomputed full board from {corner} (new) + {other_name} (fixed). "
            f"Board side={side_len * 100:.1f}cm."
        )
        self.feedback(msg)
        return msg
