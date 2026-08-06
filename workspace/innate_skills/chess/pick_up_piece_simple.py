# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pick up and place chess pieces using only calibration data and arm
orientation, without Gemini vision or base driving.

Orientation strategy by rank/column:
  Ranks 4-6:  pitch straight down, yaw centered
  Ranks 7-8:  pitch tilted, yaw centered
  Ranks 1-3:  pitch straight down, yaw left (cols A-D) or right (cols E-H)

Ranks 5-6 can reach ranks 7-8 directly with tilted orientation; any other
move across the rank 6/7 boundary relays through rank 5.
"""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from innate import Manipulation, Skill, SkillReturn, Waypoint
from innate.exceptions import ArmFailed, ArmUnhealthy, SkillCancelled

CALIBRATION_FILE = Path.home() / "board_calibration.json"
PieceType = Literal["king", "queen", "rook", "bishop", "knight", "pawn"]


class PickUpPieceSimple(Skill):
    """Pick up a piece from one square and place it on another without using
    Gemini vision or base driving.  Uses arm orientation changes to reach all
    ranks.  Parameters: square (source, e.g. 'E2'), place_square (target,
    e.g. 'E4'), piece (str, e.g. 'pawn'), is_capture (bool, True if capturing
    an opponent piece), speed (float)."""

    manipulation: Manipulation

    FIXED_ROLL = 0.0
    PITCH_DOWN = 1.45  # nearly straight down (compensates for slight arm slouch)
    PITCH_TILTED = 1.57 - 0.48  # tilted for far ranks (7-8)
    YAW_CENTER = 0.0
    YAW_LEFT = 1.57
    YAW_RIGHT = -1.57

    HEIGHT_SAFE_CLEARANCE = 0.25  # minimum z before any lateral move
    HEIGHT_SAFE = 0.15
    HEIGHT_PICK_TALL = 0.06  # king, queen
    HEIGHT_PICK_SHORT = 0.04

    GRIPPER_OPEN_PERCENT = 50
    GRIPPER_CLOSE_STRENGTH = 0.4
    GRIPPER_MIN_WAIT = 1.0

    VERTICAL_STEPS = 4

    # Phase speed multipliers (> 1.0 = faster, on top of speed)
    PHASE_AIR = 1.5
    PHASE_LIFT = 1.3
    PHASE_DESCENT_FAR = 1.2
    PHASE_DESCENT_NEAR = 0.6

    TILTED_SPEED_FACTOR = 0.75
    HEIGHT_SAFE_TILTED = 0.18

    RELAY_RANK = 5

    # Discard zone: right of column H for captured pieces
    DISCARD_SQUARES_RIGHT = 2
    DISCARD_RANK = 4.5

    TALL_PIECES = {"king", "queen"}

    def _load_calibration(self):
        if not CALIBRATION_FILE.exists():
            return None
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except Exception:
            return None

    def _square_to_position(self, square, calibration):
        """Chess notation (e.g. 'E4') -> (x, y, z) by bilinear interpolation
        of the four calibrated corners."""
        if len(square) != 2:
            return None
        file_char, rank_char = square[0].upper(), square[1]
        if file_char not in "ABCDEFGH" or rank_char not in "12345678":
            return None
        u = (ord(file_char) - ord("A")) / 7.0
        v = (int(rank_char) - 1) / 7.0

        tl, tr = calibration.get("top_left"), calibration.get("top_right")
        bl, br = calibration.get("bottom_left"), calibration.get("bottom_right")
        if not all([tl, tr, bl, br]):
            return None

        def lerp(axis):
            return (
                (1 - u) * (1 - v) * bl.get(axis, 0)
                + u * (1 - v) * br.get(axis, 0)
                + (1 - u) * v * tl.get(axis, 0)
                + u * v * tr.get(axis, 0)
            )

        return lerp("x"), lerp("y"), lerp("z")

    def _orientation_for_square(self, square):
        rank = int(square[1])
        col = square[0].upper()
        if rank >= 7:
            return self.PITCH_TILTED, self.YAW_CENTER
        if rank >= 4:
            return self.PITCH_DOWN, self.YAW_CENTER
        return self.PITCH_DOWN, (self.YAW_LEFT if col in "ABCD" else self.YAW_RIGHT)

    def _d(self, seconds: float) -> float:
        return seconds / self._speed

    def _gripper_wait(self, seconds: float):
        self.sleep(max(seconds / self._speed, self.GRIPPER_MIN_WAIT))

    def _move_arm(self, x, y, z, pitch, yaw, duration):
        """One unverified cartesian move (chess poses near joint limits may
        legitimately settle off-pose, so FK verification stays off).

        TODO: unverified means a pose that settled somewhere else still reads
        as success, so the clearance lift and the moves above the source and
        destination squares can all "succeed" with the arm off target — the
        next lateral move or descent then hits pieces. Wants per-call
        tolerances loose enough for the near-limit poses rather than off
        entirely, which needs measuring the real settle error per square.
        """
        self.manipulation.move_to(
            x,
            y,
            z,
            roll=self.FIXED_ROLL,
            pitch=pitch,
            yaw=yaw,
            duration=self._d(duration),
            tolerance_xy=None,
            tolerance_z=None,
        )

    def _vertical_move(self, x, y, from_z, to_z, pitch, yaw, caution=1.0):
        """Move vertically in VERTICAL_STEPS increments with fixed X, Y.
        Descent is fast at the top and slow near the board; lift is uniformly
        fast. Tries the smooth trajectory service, falls back to step moves.
        The gripper rides along on the standing grip target."""
        descending = to_z < from_z
        direction = "descending" if descending else "lifting"
        n = self.VERTICAL_STEPS

        waypoints = []
        for i in range(1, n + 1):
            if descending:
                frac = (i - 0.5) / n
                phase = self.PHASE_DESCENT_FAR + (self.PHASE_DESCENT_NEAR - self.PHASE_DESCENT_FAR) * frac
            else:
                phase = self.PHASE_LIFT
            waypoints.append(
                Waypoint(
                    x,
                    y,
                    from_z + (to_z - from_z) * i / n,
                    roll=self.FIXED_ROLL,
                    pitch=pitch,
                    yaw=yaw,
                    duration=1.0 / (self._speed * phase * caution),
                )
            )
        try:
            self.manipulation.follow(waypoints)
            return
        except ArmFailed as e:
            self.logger.warning(f"[PickUpPieceSimple] Trajectory failed ({e}), falling back to step-by-step")

        for i, wp in enumerate(waypoints, start=1):
            try:
                # wp.duration is already speed-scaled; pre-multiply so _move_arm's
                # own _d() divide lands back on it rather than scaling twice.
                self._move_arm(wp.x, wp.y, wp.z, wp.pitch, wp.yaw, wp.duration * self._speed)
            except (ArmFailed, ArmUnhealthy) as e:
                self.fail(f"Failed at {direction} step {i}/{n} z={wp.z:.3f}m: {e}")

    def _go_to_safe_pose(self):
        """Lift straight up first (no lateral move over the board), then fold
        to the resting safe pose. Raises on arm failure."""
        try:
            pose = self.manipulation.pose
        except ArmFailed:
            pose = None
        if pose is not None and pose.z < self.HEIGHT_SAFE_CLEARANCE:
            self._move_arm(pose.x, pose.y, self.HEIGHT_SAFE_CLEARANCE, pose.pitch, pose.yaw, 2.0)
        self._move_arm(0.05, 0.08, 0.3, 0.0, 1.57, 2.0)
        self._move_arm(0.05, 0.08, 0.11, 0.0, 1.57, 1.0)

    def _needs_relay(self, src_square, dst_square):
        src_rank, dst_rank = int(src_square[1]), int(dst_square[1])
        if src_rank in (5, 6) and dst_rank >= 7:
            return False
        if dst_rank in (5, 6) and src_rank >= 7:
            return False
        return (src_rank <= 6) != (dst_rank <= 6)

    def _discard_position(self, calibration):
        tl, tr = calibration.get("top_left"), calibration.get("top_right")
        bl, br = calibration.get("bottom_left"), calibration.get("bottom_right")
        if not all([tl, tr, bl, br]):
            return None
        sq_y = ((bl["y"] - br["y"]) + (tl["y"] - tr["y"])) / 2.0 / 7.0
        v = (self.DISCARD_RANK - 1) / 7.0
        x = (1 - v) * br["x"] + v * tr["x"]
        y = (1 - v) * br["y"] + v * tr["y"] - sq_y * self.DISCARD_SQUARES_RIGHT
        z = (1 - v) * br.get("z", 0) + v * tr.get("z", 0)
        return x, y, z

    def _relay_position(self, src_square, dst_square, calibration):
        """A relay square on rank 5, on the file of whichever square is in
        ranks 1-6 so the relay stays on the reachable side."""
        file_char = (src_square if int(src_square[1]) <= 6 else dst_square)[0].upper()
        relay_square = f"{file_char}{self.RELAY_RANK}"
        return relay_square, self._square_to_position(relay_square, calibration)

    def _do_pick_place(
        self,
        src_x,
        src_y,
        dst_x,
        dst_y,
        pick_height,
        src_pitch,
        src_yaw,
        dst_pitch,
        dst_yaw,
        src_label,
        dst_label,
        safe_height=None,
        caution=1.0,
    ):
        """One pick-and-place cycle: pick from src, place at dst. Raises
        SkillFailed on any arm failure. The standing grip target carries the
        commanded gripper closure through every move — no per-call threading."""
        if safe_height is None:
            safe_height = self.HEIGHT_SAFE
        air_dur = 2.0 / (self.PHASE_AIR * caution)

        self.feedback(f"Moving above {src_label}...")
        try:
            self._move_arm(src_x, src_y, safe_height, src_pitch, src_yaw, air_dur)
        except (ArmFailed, ArmUnhealthy) as e:
            self.fail(f"Failed to move above {src_label}: {e}")

        self.feedback("Opening gripper...")
        self.manipulation.gripper_open(self.GRIPPER_OPEN_PERCENT)
        self._gripper_wait(1.5)

        self.feedback(f"Descending to pick from {src_label}...")
        self._vertical_move(src_x, src_y, safe_height, pick_height, src_pitch, src_yaw, caution)

        self.feedback("Grabbing piece...")
        self.manipulation.gripper_close(self.GRIPPER_CLOSE_STRENGTH)
        self._gripper_wait(2.0)

        self.feedback("Lifting piece...")
        self._vertical_move(src_x, src_y, pick_height, safe_height, src_pitch, src_yaw, caution)

        self.feedback(f"Moving above {dst_label}...")
        try:
            self._move_arm(dst_x, dst_y, safe_height, dst_pitch, dst_yaw, air_dur)
        except (ArmFailed, ArmUnhealthy) as e:
            self.fail(f"Failed to move above {dst_label}: {e}")

        self.feedback(f"Descending to place on {dst_label}...")
        self._vertical_move(dst_x, dst_y, safe_height, pick_height, dst_pitch, dst_yaw, caution)

        self.feedback("Releasing piece...")
        self.manipulation.gripper_open(self.GRIPPER_OPEN_PERCENT)
        self._gripper_wait(1.5)

        self.feedback("Lifting after place...")
        self._vertical_move(dst_x, dst_y, pick_height, safe_height, dst_pitch, dst_yaw, caution)

    def execute(
        self,
        square: str,
        place_square: str,
        piece: PieceType = "pawn",
        is_capture: bool = False,
        speed: float = 1.0,
    ) -> SkillReturn:
        self._speed = max(0.1, min(speed, 3.0))
        # Not caught: every later move assumes the arm starts folded and clear
        # of the board. Continuing from an unknown position means the first
        # lateral move at HEIGHT_SAFE sweeps through the pieces.
        try:
            self._go_to_safe_pose()
        except (ArmFailed, ArmUnhealthy) as e:
            self.fail(f"Failed to reach safe pose before moving — arm position unknown: {e}")

        calibration = self._load_calibration()
        if calibration is None:
            self.fail("No calibration data found. Run board calibration first.")
        src_pos = self._square_to_position(square, calibration)
        if src_pos is None:
            self.fail(f"Invalid source square '{square}'")
        dst_pos = self._square_to_position(place_square, calibration)
        if dst_pos is None:
            self.fail(f"Invalid target square '{place_square}'")

        msg = f"Moved piece from {square} to {place_square}"
        with self._ending_at_safe_pose(msg):
            self._move_piece(square, place_square, piece, is_capture, calibration, src_pos, dst_pos)

        self.feedback(msg)
        return msg

    @contextmanager
    def _ending_at_safe_pose(self, done: str):
        """The trailing safe pose is part of the contract — the next move
        assumes the arm starts folded and clear of the board — so a miss is
        reported on every exit path, folded into the run's own outcome."""
        stranded = " the arm then failed to return to safe pose, so its position is unknown: "
        try:
            yield
        except BaseException as err:
            safe_pose_error = self._return_to_safe_pose()
            if safe_pose_error is None:
                raise
            if isinstance(err, SkillCancelled):
                raise SkillCancelled(f"Cancelled — and{stranded}{safe_pose_error}") from err
            if isinstance(err, Exception):
                self.fail(f"{err} — and{stranded}{safe_pose_error}")
            raise
        safe_pose_error = self._return_to_safe_pose()
        if safe_pose_error is not None:
            self.fail(f"{done}, but{stranded}{safe_pose_error}")

    def _return_to_safe_pose(self) -> Exception | None:
        """Trailing safe pose that reports failure instead of raising, so the
        caller decides what it may mask."""
        self.feedback("Returning to safe pose...")
        try:
            self._go_to_safe_pose()
        except (ArmFailed, ArmUnhealthy) as e:
            self.logger.warning(f"[PickUpPieceSimple] Failed to reach safe pose after move: {e}")
            return e
        return None

    def _move_piece(self, square, place_square, piece, is_capture, calibration, src_pos, dst_pos):
        src_x, src_y, src_board_z = src_pos
        dst_x, dst_y, dst_board_z = dst_pos
        base_pick_height = (
            self.HEIGHT_PICK_TALL if piece.strip().lower() in self.TALL_PIECES else self.HEIGHT_PICK_SHORT
        )
        src_pitch, src_yaw = self._orientation_for_square(square)
        dst_pitch, dst_yaw = self._orientation_for_square(place_square)
        src_rank, dst_rank = int(square[1]), int(place_square[1])

        if is_capture:
            discard_pos = self._discard_position(calibration)
            if discard_pos is None:
                self.fail("Failed to compute discard position")
            disc_x, disc_y, _disc_z = discard_pos
            cap_tilted = dst_rank >= 7
            self.feedback(f"Capturing: removing piece from {place_square}...")
            self._do_pick_place(
                dst_x,
                dst_y,
                disc_x,
                disc_y,
                self.HEIGHT_PICK_SHORT + dst_board_z,
                dst_pitch,
                dst_yaw,
                self.PITCH_DOWN,
                self.YAW_CENTER,
                place_square,
                "discard",
                safe_height=self.HEIGHT_SAFE_TILTED if cap_tilted else self.HEIGHT_SAFE,
                caution=self.TILTED_SPEED_FACTOR if cap_tilted else 1.0,
            )

        if self._needs_relay(square, place_square):
            relay_sq, relay_pos = self._relay_position(square, place_square, calibration)
            if relay_pos is None:
                self.fail("Failed to compute relay position")
            relay_x, relay_y, relay_board_z = relay_pos

            leg1_tilted = src_rank >= 7
            self.feedback(f"Relay leg 1: {square} -> {relay_sq}")
            self._do_pick_place(
                src_x,
                src_y,
                relay_x,
                relay_y,
                base_pick_height + src_board_z,
                src_pitch,
                src_yaw,
                src_pitch,
                src_yaw,
                square,
                f"relay {relay_sq}",
                safe_height=self.HEIGHT_SAFE_TILTED if leg1_tilted else None,
                caution=self.TILTED_SPEED_FACTOR if leg1_tilted else 1.0,
            )

            leg2_tilted = dst_rank >= 7
            self.feedback(f"Relay leg 2: {relay_sq} -> {place_square}")
            self._do_pick_place(
                relay_x,
                relay_y,
                dst_x,
                dst_y,
                base_pick_height + relay_board_z,
                dst_pitch,
                dst_yaw,
                dst_pitch,
                dst_yaw,
                f"relay {relay_sq}",
                place_square,
                safe_height=self.HEIGHT_SAFE_TILTED if leg2_tilted else None,
                caution=self.TILTED_SPEED_FACTOR if leg2_tilted else 1.0,
            )
            return

        cross_56_78 = (src_rank in (5, 6) and dst_rank >= 7) or (dst_rank in (5, 6) and src_rank >= 7)
        if cross_56_78:
            src_pitch, src_yaw = self.PITCH_TILTED, self.YAW_CENTER
            dst_pitch, dst_yaw = self.PITCH_TILTED, self.YAW_CENTER
        any_tilted = src_rank >= 7 or dst_rank >= 7 or cross_56_78
        self._do_pick_place(
            src_x,
            src_y,
            dst_x,
            dst_y,
            base_pick_height + src_board_z,
            src_pitch,
            src_yaw,
            dst_pitch,
            dst_yaw,
            square,
            place_square,
            safe_height=self.HEIGHT_SAFE_TILTED if any_tilted else None,
            caution=self.TILTED_SPEED_FACTOR if any_tilted else 1.0,
        )
