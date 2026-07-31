# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
import time
from pathlib import Path
from typing import Any, Literal, cast

import chess
from google import genai
from google.genai import types

from innate import Head, Image, MainImage, Manipulation, Skill, SkillReturn, WristImage, resource

GAME_STATE_FILE = Path.home() / "chess_game_state.json"
CALIBRATION_FILE = Path.home() / "board_calibration.json"
DATA_DIR = Path.home() / "innate-os/data/detect_move"

# Handicap: White starts without the a1 rook (no queenside castling)
HANDICAP_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/1NBQKBNR w Kkq - 0 1"
ROBOT_COLORS = ("white", "black")
RobotColor = Literal["white", "black"]


def _load_env_file(env_path: Path) -> dict:
    env_vars = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env_vars[key.strip()] = value.strip()
    return env_vars


def _fen_to_ascii(fen: str) -> str:
    board = chess.Board(fen)
    lines = []
    for rank in range(7, -1, -1):
        row = [(p.symbol() if (p := board.piece_at(chess.square(file, rank))) else ".") for file in range(8)]
        lines.append(f"  {rank + 1}  {' '.join(row)}")
    lines.append("     a b c d e f g h")
    return "\n".join(lines)


class DetectOpponentMove(Skill):
    """Detect the opponent's last chess move. The robot positions its wrist
    camera above the board, captures images from both the main and wrist
    cameras, then asks Gemini which legal move was played. The board state
    (FEN) is stored in ~/chess_game_state.json. Optionally pass
    robot_color='white' or 'black' to set perspective."""

    manipulation: Manipulation
    head: Head | None
    # `| None`: detection degrades to whichever camera is live; execute()
    # fails only when BOTH are absent.
    main_image: MainImage | None
    wrist_image: WristImage | None

    OBS_Z = 0.18
    OBS_PITCH = 1.37
    OBS_YAW = 0.0
    FIXED_ROLL = 0.0
    HEAD_TILT_DOWN = -20
    HEAD_TILT_NEUTRAL = 0
    CONFIDENCE_THRESHOLD = 0.7

    # Board centre X/Y, loaded from calibration at run start; None until calibrated.
    obs_x: float | None = None
    obs_y: float | None = None

    @resource
    def gemini_client(self):
        env_vars = _load_env_file(Path(__file__).parents[1] / ".env.scan")
        api_key = env_vars.get("GEMINI_API_KEY", "")
        if not api_key or api_key == "your_gemini_api_key_here":
            self.logger.warning("[DetectOpponentMove] GEMINI_API_KEY not set in .env.scan")
            return None
        return genai.Client(api_key=api_key)

    def _load_board_center(self):
        """Board centre X/Y from calibration corners; None until calibrated."""
        self.obs_x = None
        self.obs_y = None
        if not CALIBRATION_FILE.exists():
            self.logger.warning("[DetectOpponentMove] No board calibration yet — run board calibration before use")
            return
        try:
            cal = json.loads(CALIBRATION_FILE.read_text())
            corners = [cal[k] for k in ("top_left", "top_right", "bottom_left", "bottom_right")]
            self.obs_x = sum(c["x"] for c in corners) / 4.0
            self.obs_y = sum(c["y"] for c in corners) / 4.0
        except Exception as e:
            self.logger.warning(f"[DetectOpponentMove] Unreadable board calibration ({e}) — re-run board calibration")

    def _load_game_state(self) -> dict | None:
        if not GAME_STATE_FILE.exists():
            return None
        try:
            return json.loads(GAME_STATE_FILE.read_text())
        except Exception as e:
            self.logger.error(f"[DetectOpponentMove] Failed to load game state: {e}")
            return None

    def _init_game_state(self, robot_color: str) -> dict:
        state = {
            "fen": HANDICAP_FEN,
            "move_history": [],
            "last_detected_move": None,
            "turn": "white",
            "robot_color": robot_color,
        }
        GAME_STATE_FILE.write_text(json.dumps(state, indent=2))
        return state

    def _move_to_observation_pose(self) -> bool:
        if self.obs_x is None or self.obs_y is None:
            self.logger.error("[DetectOpponentMove] No board calibration — cannot position arm")
            return False
        self.manipulation.open_gripper(100)
        self.sleep(0.5)
        if self.head:
            self.head.set_position(self.HEAD_TILT_DOWN)
            self.sleep(0.5)
        self.feedback("Moving arm to observation pose...")
        success = self.manipulation.move_to_cartesian_pose(
            x=self.obs_x,
            y=self.obs_y,
            z=self.OBS_Z,
            roll=self.FIXED_ROLL,
            pitch=self.OBS_PITCH,
            yaw=self.OBS_YAW,
            duration=2.0,
            blocking=True,
        )
        if success:
            self.sleep(0.5)  # let camera auto-exposure settle
        return success

    def _go_to_safe_pose(self):
        self.manipulation.move_to_cartesian_pose(
            x=0.15,
            y=0.1,
            z=0.1,
            roll=self.FIXED_ROLL,
            pitch=self.OBS_PITCH,
            yaw=self.OBS_YAW,
            duration=2.0,
            blocking=True,
        )
        if self.head:
            self.head.set_position(self.HEAD_TILT_NEUTRAL)

    def _capture_images(self) -> tuple[MainImage | None, WristImage | None]:
        self.sleep(0.3)
        main_b64 = self.main_image
        wrist_b64 = self.wrist_image
        self._save_debug("capture", None, main_b64, wrist_b64)
        return main_b64, wrist_b64

    def _build_board_context(self, fen: str) -> tuple:
        board = chess.Board(fen)
        legal_moves = [m.uci() for m in board.legal_moves]
        turn = "White" if board.turn == chess.WHITE else "Black"
        context = (
            f"CURRENT BOARD STATE (FEN): {fen}\n\n"
            f"ASCII board:\n{_fen_to_ascii(fen)}\n\n"
            f"It is {turn}'s turn.\n"
            f"Legal moves ({len(legal_moves)}): {', '.join(legal_moves)}\n"
        )
        return context, legal_moves, board

    def _save_debug(self, label: str, text: str | None, main_b64: Image | None, wrist_b64: Image | None) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            if text:
                (DATA_DIR / f"{label}_{ts}.txt").write_text(text)
            if main_b64:
                (DATA_DIR / f"{label}_main_{ts}.jpg").write_bytes(main_b64.jpeg)
            if wrist_b64:
                (DATA_DIR / f"{label}_wrist_{ts}.jpg").write_bytes(wrist_b64.jpeg)
        except Exception as e:
            self.logger.warning(f"[DetectOpponentMove] Failed to save {label} debug files: {e}")

    def _ask_gemini(self, label: str, model: str, thinking_budget: int, prompt: str, wrist_b64) -> dict | None:
        """Ask for {move_uci, confidence, reasoning}; None on failure."""
        client = self.gemini_client
        if client is None:  # execute() fails before this can happen; keeps the deref safe
            return None
        contents: list[Any] = [prompt]
        if wrist_b64:
            contents.append(types.Part.from_bytes(data=wrist_b64.jpeg, mime_type="image/jpeg"))
        self._save_debug(f"{label}_prompt", prompt, None, wrist_b64)
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
                ),
            )
            result = json.loads((response.text or "").strip())
            self.logger.info(f"[DetectOpponentMove] {label} result: {result}")
            return result
        except Exception as e:
            self.logger.error(f"[DetectOpponentMove] Gemini {label} failed: {e}")
            return None

    def _detect_prompt(self, board_context: str) -> str:
        return (
            "You are analyzing a physical chess board to detect the opponent's last move.\n\n"
            f"{board_context}\n"
            "I am providing a close-up overhead wrist camera image of the board.\n\n"
            "Compare the CURRENT FEN state (the state BEFORE the opponent moved) "
            "with what you see in the images (the state AFTER the opponent moved).\n"
            "Identify which piece moved and where it went.\n\n"
            "IMPORTANT: The move MUST be one of the legal moves listed above.\n\n"
            "Return ONLY JSON:\n"
            '{"move_uci": "<from_square><to_square>", "confidence": 0.0-1.0, '
            '"reasoning": "brief explanation"}'
        )

    def _confirm_prompt(self, board_context: str, candidates: list) -> str:
        return (
            "You are analyzing a physical chess board to confirm which move was played.\n\n"
            f"{board_context}\n"
            f"The most likely moves are: {', '.join(candidates)}\n\n"
            "I am providing a close-up overhead wrist camera image of the board.\n"
            "Look carefully at which squares changed and which piece is now where.\n\n"
            "Return ONLY JSON:\n"
            '{"move_uci": "<from_square><to_square>", "confidence": 0.0-1.0, '
            '"reasoning": "brief explanation"}'
        )

    def execute(self, robot_color: RobotColor = "white") -> SkillReturn:
        robot_color = cast(RobotColor, robot_color.strip().lower())
        if robot_color not in ROBOT_COLORS:
            self.fail(f"Invalid robot_color '{robot_color}'. Must be 'white' or 'black'.")
        if self.gemini_client is None:
            self.fail("Gemini not configured (check .env.scan)")
        self._load_board_center()

        state = self._load_game_state()
        if state is None:
            self.feedback("No game state found — initialising new game.")
            state = self._init_game_state(robot_color)
        fen = state["fen"]
        self.feedback(f"Current board state loaded. Turn: {state.get('turn', '?')}")

        if not self._move_to_observation_pose():
            self.fail("Failed to move arm to observation pose")
        try:
            return self._detect(fen)
        finally:
            self._go_to_safe_pose()

    def _detect(self, fen: str) -> str:
        self.feedback("Capturing images...")
        main_b64, wrist_b64 = self._capture_images()
        if not main_b64 and not wrist_b64:
            self.fail("No camera images available")

        board_context, legal_moves, board = self._build_board_context(fen)
        if not legal_moves:
            return "No legal moves — game may be over"

        self.feedback(f"Asking Gemini to identify the move ({len(legal_moves)} legal moves)...")
        result = self._ask_gemini(
            "stage1", "gemini-3.1-pro-preview", 1024, self._detect_prompt(board_context), wrist_b64
        )
        if result is None:
            self.fail("Gemini failed to analyse the board")

        move_uci = result.get("move_uci", "")
        confidence = float(result.get("confidence", 0.0))
        reasoning = result.get("reasoning", "")

        if confidence < self.CONFIDENCE_THRESHOLD and move_uci in legal_moves:
            self.feedback(f"Low confidence ({confidence:.0%}) on {move_uci}. Running confirmation...")
            candidates = [move_uci]
            candidates += [
                m for m in legal_moves if m != move_uci and (m[:2] == move_uci[:2] or m[2:4] == move_uci[2:4])
            ]
            candidates += [m for m in legal_moves if m not in candidates]
            result2 = self._ask_gemini(
                "stage2", "gemini-3-flash-preview", 512, self._confirm_prompt(board_context, candidates[:8]), wrist_b64
            )
            if result2:
                move2 = result2.get("move_uci", "")
                conf2 = float(result2.get("confidence", 0.0))
                if conf2 > confidence and move2 in legal_moves:
                    move_uci, confidence = move2, conf2
                    reasoning = result2.get("reasoning", reasoning)

        if move_uci not in legal_moves:
            self.fail(f"Detected move {move_uci} is not legal. Legal moves: {legal_moves}")

        san = board.san(chess.Move.from_uci(move_uci))
        msg = (
            f"Detected opponent move: {san} ({move_uci}). "
            f"Confidence: {confidence:.0%}. {reasoning}. "
            f'Call update_chess_state(move_uci="{move_uci}") to apply it.'
        )
        self.feedback(msg)
        return msg
