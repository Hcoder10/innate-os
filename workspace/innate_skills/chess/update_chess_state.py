# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import json
from pathlib import Path

import chess

from innate import Skill, SkillReturn

GAME_STATE_FILE = Path.home() / "chess_game_state.json"


class UpdateChessState(Skill):
    """Apply a chess move (UCI notation, e.g. 'e2e4') to the game state.
    Validates the move is legal, updates the FEN and move history in
    ~/chess_game_state.json. Call this after every move — both the robot's
    own moves and detected opponent moves."""

    def execute(self, move_uci: str) -> SkillReturn:
        move_uci = move_uci.strip().lower()

        if not GAME_STATE_FILE.exists():
            self.fail("No game state found. Call reset_chess_game first.")
        try:
            state = json.loads(GAME_STATE_FILE.read_text())
        except Exception as e:
            self.fail(f"Failed to load game state: {e}")

        board = chess.Board(state.get("fen", chess.STARTING_FEN))
        try:
            move = chess.Move.from_uci(move_uci)
        except ValueError:
            self.fail(f"Invalid UCI notation: '{move_uci}'")
        if move not in board.legal_moves:
            self.fail(f"Move {move_uci} is not legal. Legal moves: {[m.uci() for m in board.legal_moves]}")

        san = board.san(move)
        board.push(move)
        turn = "white" if board.turn == chess.WHITE else "black"
        new_state = {
            "fen": board.fen(),
            "move_history": [*state.get("move_history", []), move_uci],
            "last_move": move_uci,
            "turn": turn,
            "robot_color": state.get("robot_color", "white"),
        }
        try:
            GAME_STATE_FILE.write_text(json.dumps(new_state, indent=2))
        except Exception as e:
            self.fail(f"Failed to save game state: {e}")

        status = ""
        if board.is_checkmate():
            winner = "Black" if board.turn == chess.WHITE else "White"
            status = f" Checkmate — {winner} wins!"
        elif board.is_stalemate():
            status = " Stalemate — draw!"
        elif board.is_check():
            status = " Check!"

        msg = f"Applied {san} ({move_uci}). Turn: {turn}. FEN: {board.fen()}{status}"
        self.feedback(msg)
        return msg
